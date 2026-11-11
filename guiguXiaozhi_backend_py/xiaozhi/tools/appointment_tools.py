"""挂号相关工具。

设计要点（都来自 Java 版的实测教训）：
  * 入参用**扁平字符串参数**，不用嵌套对象 —— 小模型对嵌套 schema 映射能力差；
  * 每个可选参数都标 ``required=False`` —— 多一个必填项，模型就会整个不调工具；
  * 工具内部自己校验参数，**校验失败把错误信息回传给模型**让它自我纠正，
    而不是抛异常打断整轮对话；
  * 工具名与描述保持稳定，模型靠它们做路由。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from xiaozhi.core.tool import Tool, ToolDefinition, ToolParameter, RiskLevel
from xiaozhi.repositories.appointment_repo import Appointment, AppointmentRepository
from xiaozhi.security.validators import validate_appointment
from xiaozhi.service.appointment_service import AppointmentService, BookingOutcome
from xiaozhi.service.doctor_service import DoctorService
from xiaozhi.service.slot_service import SlotService
from xiaozhi.tools.argument_normalizer import normalize_arguments

logger = logging.getLogger(__name__)


def build_department_hint(doctor_service: DoctorService) -> str:
    """列出本院真实科室，供工具描述里给模型参考。

    2026-11-10 真实联调：用户问"内科"，本院没有泛化的"内科"，
    模型被拒后**编了一个"呼吸科"**（真名是"呼吸内科"），
    用户确认"可以"后才被工具再拒一次 —— 用户白等两轮。
    把清单给模型，它就不需要猜了。
    """
    try:
        departments = sorted(doctor_service.list_departments() or [])
    except Exception:
        departments = []
    if not departments:
        return ""
    return (
        "本院科室有："
        + "、".join(departments)
        + "。请从中选择最接近的一个；用户说的若不是其中之一（如“内科”），"
        "应先向用户说明本院没有该科室并推荐最接近的科室，让用户确认后再预约。"
    )


class BookAppointmentTool(Tool):
    """提交预约"""

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="预约挂号",
            description=(
                "【提交预约】当用户要求创建预约、提交挂号、或说“帮我预约/帮我挂号/确认预约”时，"
                "调用本工具真正创建预约记录。"
                "入参：患者姓名、身份证号、预约科室、预约日期(格式如2026-11-20)、预约时段(上午或下午)、预约医生(可选)。"
                "本工具内部会先查询号源，号源充足才会写入预约记录。"
                "如果用户只是询问有没有号源而尚未要求提交预约，请改用“查询是否有号源”。"
                + self._department_hint
            ),
            parameters=[
                ToolParameter("username", "string", "患者姓名"),
                ToolParameter("id_card", "string", "患者身份证号"),
                ToolParameter(
                    "department",
                    "string",
                    "预约科室名称，必须是本院真实科室名（见下方清单），不要自己编造或简写",
                ),
                ToolParameter("date", "string", "预约日期，格式：2026-11-20"),
                ToolParameter("time", "string", "预约时段，只能是：上午 或 下午"),
                ToolParameter("doctor_name", "string", "预约医生姓名，可选；用户没指定时可留空", required=False),
            ],
            risk_level=RiskLevel.WRITE,
            permissions_required=["appointment:write"],
            # Phase 2：写操作契约 —— 必须经用户明确确认、幂等、结果可核验
            approval_required=True,
            idempotency_required=True,
            result_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
            retry_policy="query_before_retry",
        )

    def __init__(
        self,
        repository: AppointmentRepository,
        slot_service: SlotService,
        doctor_service: DoctorService,
    ) -> None:
        self.repository = repository
        self.slot_service = slot_service
        self.doctor_service = doctor_service
        # Phase 3a：业务规则（状态机、原子查重写入、异常分类）收敛到服务层；
        # 工具层只负责"参数校验 → 调服务 → 转述结果"。
        self._service = AppointmentService(repository, slot_service)
        # 本院真实科室清单 —— 注入工具描述，避免模型自己编科室名。
        # 2026-11-10 真实联调：用户问"内科"，本院没有泛化的"内科"，
        # 模型被拒后**编了一个"呼吸科"**（真名是"呼吸内科"），
        # 用户确认"可以"后才被工具再拒一次，用户白等两轮。
        self._department_hint = self._build_department_hint()

    def _build_department_hint(self) -> str:
        """列出本院科室，供模型选择"""
        return build_department_hint(self.doctor_service)

    async def call(self, params: Dict[str, Any]) -> Any:
        from xiaozhi.core.tool import ToolResult

        departments = self.doctor_service.list_departments() or None
        data_in = normalize_arguments(
            params, ["username", "id_card", "department", "date", "time", "doctor_name"]
        )
        validation = validate_appointment(data_in, known_departments=departments)
        if not validation.ok:
            # 校验错误回传给模型，让它自己纠正后重试（不要抛异常打断）
            return ToolResult.fail(
                f"参数校验未通过：{validation.message()}。请根据提示修正参数后重新调用。",
                error_type="ValidationError",
            )

        data = validation.normalized

        # ---- 姓名/医生名**字段错位**防护 ----
        # 2026-11-10 真实联调：用户回"王新勃，610121199401032831，2026.11.11，上午"
        # （王新勃是**医生**名，患者姓名还没给），小模型把"王新勃"填进了
        # username（患者姓名），doctor_name 留空 → 工具随手选了第一个出诊医生，
        # **用医生名建了一条患者预约记录**。这是实打实的数据事故。
        #
        # 这里做一次交叉校验：username 命中了本科室的医生名录，
        # 而 doctor_name 为空 → 几乎必然是字段填反了，直接拒绝并让模型重问。
        swap_error = self._detect_name_swap(data)
        if swap_error is not None:
            return ToolResult.fail(swap_error, error_type="ValidationError")

        # ---- 委托业务服务（Phase 3a）：号源检查 + 原子查重写入都在服务里 ----
        result = self._service.book(
            username=data["username"],
            id_card=data["id_card"],
            department=data["department"],
            date=data["date"],
            time=data["time"],
            doctor_name=data.get("doctor_name"),
        )

        if result.outcome == BookingOutcome.CREATED:
            return ToolResult.ok(
                f"预约成功。预约详情：{self._describe(result.appointment)}"
            )
        if result.outcome == BookingOutcome.ALREADY_EXISTS:
            existing = result.appointment
            doctor_text = f"（医生：{existing.doctor_name or '未指定'}）" if existing else ""
            return ToolResult.fail(
                f"您在{data['department']} {data['date']} {data['time']}"
                f"已有预约{doctor_text}，请勿重复预约。",
                error_type="DuplicateAppointment",
            )
        if result.outcome == BookingOutcome.NO_SLOT:
            return ToolResult.fail(
                f"{result.message} 因此无法为您办理预约。", error_type="NoSlotAvailable"
            )
        if result.outcome == BookingOutcome.REPOSITORY_UNAVAILABLE:
            # 基础设施不可用 → 如实说系统问题，不伪装成业务失败
            return ToolResult.fail(
                f"预约未能写入：{result.message}", error_type="RepositoryUnavailable"
            )
        if result.outcome == BookingOutcome.UNKNOWN:
            # Phase 4：结果未核实（HIS 对账语义）→ 结构化标记 UNKNOWN，
            # Response Gate 收尾时换固定文案"结果核实中，请勿重复提交"，
            # 绝不允许模型宣称成功或失败。
            return ToolResult.fail(
                result.message or "结果核实中，请勿重复提交。",
                error_type="UnknownState",
                authoritative_status="UNKNOWN",
            )
        return ToolResult.fail(
            f"预约写入失败：{result.message}", error_type="PersistenceError"
        )

    def _detect_name_swap(self, data: Dict[str, Any]) -> Optional[str]:
        """检测"患者姓名"和"医生姓名"填反了。

        返回错误说明（没问题时返回 None）。

        判据：``username`` 命中了本科室专家名录里的某个医生，
        且 ``doctor_name`` 为空 —— 说明模型把医生名当成了患者名。
        """
        username = (data.get("username") or "").strip()
        doctor_name = (data.get("doctor_name") or "").strip()
        if not username or doctor_name:
            return None
        try:
            roster = self.doctor_service.list_doctors(data.get("department") or "")
        except Exception:  # 名录不可用时不做判断，放行
            return None
        matched = next((d for d in roster if d.name == username), None)
        if matched is None:
            return None
        return (
            f"参数填写有误：{username} 是本院{matched.department}的**医生**，"
            f"不能作为患者姓名。请把患者姓名填入 username，"
            f"医生姓名填入 doctor_name；如果用户还没提供患者姓名，"
            f"请先向用户询问患者姓名，不要用医生名代替。"
        )

    @staticmethod
    def _describe(appointment: Appointment) -> str:
        return (
            f"姓名：{appointment.username}，身份证号：{appointment.id_card}，"
            f"科室：{appointment.department}，日期：{appointment.date}，"
            f"时段：{appointment.time}，医生：{appointment.doctor_name or '未指定'}"
        )


class CancelAppointmentTool(Tool):
    """取消预约"""

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="取消预约挂号",
            description=(
                "【取消预约】当用户要求取消、退掉、撤销已有的挂号预约时调用本工具。"
                "入参：患者姓名、身份证号、预约科室、预约日期(格式如2026-11-20)、预约时段(上午或下午)。"
            ),
            parameters=[
                ToolParameter("username", "string", "患者姓名"),
                ToolParameter("id_card", "string", "患者身份证号"),
                ToolParameter(
                    "department",
                    "string",
                    "预约科室名称，必须是本院真实科室名（见本工具描述里的科室清单），不要自己编造或简写",
                ),
                ToolParameter("date", "string", "预约日期，格式：2026-11-20"),
                ToolParameter("time", "string", "预约时段，只能是：上午 或 下午"),
            ],
            risk_level=RiskLevel.DESTRUCTIVE,
            permissions_required=["appointment:cancel"],
            # Phase 2：删除类副作用同样必须确认；幂等由仓储查重兜底
            approval_required=True,
            idempotency_required=True,
            result_schema={"type": "object", "properties": {"success": {"type": "boolean"}}},
            retry_policy="query_before_retry",
        )

    def __init__(self, repository: AppointmentRepository) -> None:
        self.repository = repository
        # Phase 3a：取消 = 状态迁移，业务规则在服务层。
        # 取消不需要号源服务（slot_service 传 None）；
        # 本工具只用 cancel，book 的号源检查走 BookAppointmentTool。
        self._service = AppointmentService(repository, None)

    async def call(self, params: Dict[str, Any]) -> Any:
        from xiaozhi.core.tool import ToolResult

        data_in = normalize_arguments(
            params, ["username", "id_card", "department", "date", "time", "doctor_name"]
        )
        validation = validate_appointment(data_in)
        if not validation.ok:
            return ToolResult.fail(
                f"参数校验未通过：{validation.message()}。请根据提示修正参数后重新调用。",
                error_type="ValidationError",
            )

        data = validation.normalized

        # ---- 委托业务服务（Phase 3a）：取消 = 状态迁移，保留审计痕迹 ----
        result = self._service.cancel(
            username=data["username"],
            id_card=data["id_card"],
            department=data["department"],
            date=data["date"],
            time=data["time"],
        )
        if result.outcome == BookingOutcome.CREATED:
            cancelled = result.appointment
            return ToolResult.ok(
                "取消预约成功："
                f"姓名：{cancelled.username}，科室：{cancelled.department}，"
                f"日期：{cancelled.date}，时段：{cancelled.time}"
            )
        if result.outcome == BookingOutcome.REPOSITORY_UNAVAILABLE:
            return ToolResult.fail(
                f"取消预约未能完成：{result.message}",
                error_type="RepositoryUnavailable",
            )
        if result.outcome == BookingOutcome.UNKNOWN:
            return ToolResult.fail(
                result.message or "结果核实中，请勿重复提交。",
                error_type="UnknownState",
                authoritative_status="UNKNOWN",
            )
        if result.outcome == BookingOutcome.FAILED:
            # 业务层已经给了可读文案（未找到 / 已取消 / 失败）
            error_type = "NotFound" if "未找到" in result.message else "PersistenceError"
            return ToolResult.fail(result.message, error_type=error_type)
        return ToolResult.fail(
            f"取消预约失败：{result.message}", error_type="PersistenceError"
        )


class QuerySlotTool(Tool):
    """查询号源"""

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="查询是否有号源",
            description=(
                "【仅查询号源】当用户只是询问某个科室某天上午或下午还有没有号、能不能挂号，"
                "**并且没有表达要提交预约**时，调用本工具查询。"
                "入参：科室名称、日期(格式如2026-11-20)、时段(上午或下午)、医生姓名(可选)。"
                "如果用户已经说了“我要预约/帮我挂号”，请改用“预约挂号”。"
                + build_department_hint(self.doctor_service)
            ),
            parameters=[
                ToolParameter(
                    "department",
                    "string",
                    "科室名称，必须是本院真实科室名（见本工具描述里的科室清单）",
                ),
                ToolParameter("date", "string", "日期，格式：2026-11-20"),
                ToolParameter("time", "string", "时段，只能是：上午 或 下午"),
                ToolParameter("doctor_name", "string", "医生姓名，可选", required=False),
            ],
            risk_level=RiskLevel.READ_ONLY,
            permissions_required=[],
        )

    def __init__(
        self,
        slot_service: SlotService,
        doctor_service: DoctorService,
    ) -> None:
        self.slot_service = slot_service
        self.doctor_service = doctor_service

    async def call(self, params: Dict[str, Any]) -> Any:
        from xiaozhi.core.tool import ToolResult

        data_in = normalize_arguments(
            params, ["department", "date", "time", "doctor_name"]
        )
        department = str(data_in.get("department") or "").strip()
        date_str = str(data_in.get("date") or "").strip()
        time_slot = str(data_in.get("time") or "").strip()

        # ---- 参数先行校验，缺什么直接 fail 并给出纠正指引 ----
        # 2026-11-10 第五轮联调实测：模型漏传时段，slot_service 提前返回
        # "时段只能是上午或下午"，本工具却把它包装成
        # ok("暂时没有可预约的号源。时段只能是…") → 模型转述成
        # "内科没有号源"，用户被误导成"号源查过了、确实没有"。
        # 参数问题必须让模型**去问用户**，而不是当作查询结果。
        try:
            departments = sorted(self.doctor_service.list_departments() or [])
        except Exception:
            departments = []
        if not department:
            return ToolResult.fail(
                "缺少科室名称。请向用户询问要查询哪个科室的号源。",
                error_type="ValidationError",
            )
        if departments and department not in departments:
            return ToolResult.fail(
                f"本院没有【{department}】这个科室。本院科室有：{'、'.join(departments)}。"
                "请先向用户说明本院没有该科室，推荐最接近的真实科室，"
                "等用户确认后再查询，不要把不存在当成'没有号源'。",
                error_type="ValidationError",
            )
        if not date_str:
            return ToolResult.fail(
                "缺少就诊日期。请向用户询问就诊日期（格式如 2026-11-20）。",
                error_type="ValidationError",
            )
        if not time_slot:
            return ToolResult.fail(
                "缺少预约时段。请向用户询问是上午还是下午，确认后再查询号源。",
                error_type="ValidationError",
            )

        availability = self.slot_service.check(
            department, date_str, time_slot, data_in.get("doctor_name")
        )
        if availability.available:
            return ToolResult.ok(availability.message)
        # 不可用的真实原因（周末不出诊 / 已约满 / 暂无排班 / 科室不存在）
        # message 里已经说清楚了，直接转述即可——
        # 不要再套"暂时没有可预约的号源"这种一概而论的误导前缀。
        return ToolResult.ok(availability.message)


class QueryDoctorsTool(Tool):
    """查询科室专家"""

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="查询科室专家",
            description=(
                "【查询专家】当用户询问某个科室有哪些出诊专家、哪位医生擅长某类疾病时，"
                "调用本工具获取权威名单，不要凭记忆直接回答。"
                "入参：科室名称，擅长方向关键词(可选)。"
            ),
            parameters=[
                ToolParameter(
                    "department",
                    "string",
                    "科室名称，必须是本院真实科室名（见本工具描述里的科室清单）",
                ),
                ToolParameter("keyword", "string", "擅长方向关键词，可选，例如：脑血管病", required=False),
            ],
            risk_level=RiskLevel.READ_ONLY,
            permissions_required=[],
        )

    def __init__(self, doctor_service: DoctorService) -> None:
        self.doctor_service = doctor_service

    async def call(self, params: Dict[str, Any]) -> Any:
        from xiaozhi.core.tool import ToolResult

        data_in = normalize_arguments(params, ["department", "keyword"])
        department = data_in.get("department", "")
        keyword = (data_in.get("keyword") or "").strip()
        doctors = self.doctor_service.list_doctors(department)
        if not doctors:
            return ToolResult.ok(
                f"本院暂无【{department}】科室的专家信息，建议先做 AI 分导诊。"
            )
        matched = [
            d
            for d in doctors
            if not keyword or keyword in d.expertise or keyword in d.name
        ]
        if not matched:
            return ToolResult.ok(
                f"{department}没有擅长“{keyword}”的医生。可选医生有："
                + "、".join(d.brief() for d in doctors)
            )
        lines = [f"{department}的出诊专家有："]
        lines.extend(f"{i}. {d.describe()}" for i, d in enumerate(matched, start=1))
        return ToolResult.ok("\n".join(lines))
