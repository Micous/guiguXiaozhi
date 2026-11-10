"""挂号相关工具。

设计要点（都来自 Java 版的实测教训）：
  * 入参用**扁平字符串参数**，不用嵌套对象 —— 小模型对嵌套 schema 映射能力差；
  * 每个可选参数都标 ``required=False`` —— 多一个必填项，模型就会整个不调工具；
  * 工具内部自己校验参数，**校验失败把错误信息回传给模型**让它自我纠正，
    而不是抛异常打断整轮对话；
  * 工具名与描述保持稳定，模型靠它们做路由。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from xiaozhi.core.tool import Tool, ToolDefinition, ToolParameter, RiskLevel
from xiaozhi.repositories.appointment_repo import Appointment, AppointmentRepository
from xiaozhi.security.validators import validate_appointment
from xiaozhi.service.doctor_service import DoctorService
from xiaozhi.service.slot_service import SlotService
from xiaozhi.tools.argument_normalizer import normalize_arguments


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
            ),
            parameters=[
                ToolParameter("username", "string", "患者姓名"),
                ToolParameter("id_card", "string", "患者身份证号"),
                ToolParameter("department", "string", "预约科室名称，例如：神经内科"),
                ToolParameter("date", "string", "预约日期，格式：2026-11-20"),
                ToolParameter("time", "string", "预约时段，只能是：上午 或 下午"),
                ToolParameter("doctor_name", "string", "预约医生姓名，可选；用户没指定时可留空", required=False),
            ],
            risk_level=RiskLevel.WRITE,
            permissions_required=["appointment:write"],
        )

    def __init__(
        self,
        repository: AppointmentRepository,
        slot_service: SlotServiceProtocol,
        doctor_service: DoctorService,
    ) -> None:
        self.repository = repository
        self.slot_service = slot_service
        self.doctor_service = doctor_service

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
        availability = self.slot_service.check(
            data["department"], data["date"], data["time"], data.get("doctor_name")
        )
        if not availability.available:
            return ToolResult.fail(
                f"{availability.message} 因此无法为您办理预约。", error_type="NoSlotAvailable"
            )

        doctor = data.get("doctor_name") or (
            availability.doctors[0] if availability.doctors else None
        )
        appointment = Appointment(
            username=data["username"],
            id_card=data["id_card"],
            department=data["department"],
            date=data["date"],
            time=data["time"],
            doctor_name=doctor,
        )

        duplicate = self.repository.find_duplicate(appointment)
        if duplicate is not None:
            return ToolResult.fail(
                f"您在{appointment.department} {appointment.date} {appointment.time}"
                f"已有预约（医生：{duplicate.doctor_name or '未指定'}），请勿重复预约。",
                error_type="DuplicateAppointment",
            )

        saved = self.repository.save(appointment)
        if saved is None:
            return ToolResult.fail("预约写入失败，请稍后重试。", error_type="PersistenceError")
        return ToolResult.ok(f"预约成功。预约详情：{self._describe(saved)}")

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
                ToolParameter("department", "string", "预约科室名称，例如：神经内科"),
                ToolParameter("date", "string", "预约日期，格式：2026-11-20"),
                ToolParameter("time", "string", "预约时段，只能是：上午 或 下午"),
            ],
            risk_level=RiskLevel.DESTRUCTIVE,
            permissions_required=["appointment:cancel"],
        )

    def __init__(self, repository: AppointmentRepository) -> None:
        self.repository = repository

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
        probe = Appointment(
            username=data["username"],
            id_card=data["id_card"],
            department=data["department"],
            date=data["date"],
            time=data["time"],
        )
        existing = self.repository.find_duplicate(probe)
        if existing is None or existing.id is None:
            return ToolResult.fail(
                "未找到可取消的预约记录，请核对姓名、身份证号、科室、日期和时段。",
                error_type="NotFound",
            )
        if not self.repository.delete(existing.id):
            return ToolResult.fail("取消预约失败，请稍后重试。", error_type="PersistenceError")
        return ToolResult.ok(
            "取消预约成功，已删除："
            f"姓名：{existing.username}，科室：{existing.department}，"
            f"日期：{existing.date}，时段：{existing.time}"
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
            ),
            parameters=[
                ToolParameter("department", "string", "科室名称，例如：神经内科"),
                ToolParameter("date", "string", "日期，格式：2026-11-20"),
                ToolParameter("time", "string", "时段，只能是：上午 或 下午"),
                ToolParameter("doctor_name", "string", "医生姓名，可选", required=False),
            ],
            risk_level=RiskLevel.READ_ONLY,
            permissions_required=[],
        )

    def __init__(self, slot_service: SlotServiceProtocol) -> None:
        self.slot_service = slot_service

    async def call(self, params: Dict[str, Any]) -> Any:
        from xiaozhi.core.tool import ToolResult

        data_in = normalize_arguments(
            params, ["department", "date", "time", "doctor_name"]
        )
        availability = self.slot_service.check(
            data_in.get("department", ""),
            data_in.get("date", ""),
            data_in.get("time", ""),
            data_in.get("doctor_name"),
        )
        if availability.available:
            return ToolResult.ok(availability.message)
        return ToolResult.ok(f"暂时没有可预约的号源。{availability.message}")


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
                ToolParameter("department", "string", "科室名称，例如：神经内科"),
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
