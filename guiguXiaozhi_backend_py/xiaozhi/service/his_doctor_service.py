"""HIS 院方名录适配层 —— 让「查询科室 / 查询医生」工具只读 HIS。

**为什么需要这一层**

``DoctorService`` 是工具层依赖的抽象（``list_departments`` /
``list_doctors`` / ``find_doctor``）。改造前的实现是
``VectorStoreDoctorService``：从知识库 ``doctors.md`` 经 Milvus 检索解析出
科室与医生 —— 这是**HIS 之外的第二份院方数据**，会与号源排班漂移，且将来
接真实医院 HIS 时，工具层、提示词、名录服务都得跟着改。

本类实现同一个抽象，数据全部来自 HIS 的 ``/dept/list`` 与 ``/doctor/list``。
于是：

.. code-block:: text

    预约挂号 / 取消预约挂号 / 查询号源  ─┐
    查询科室 / 查询科室专家 / 姓名错位校验 ├─> HisClient ─> HIS（唯一数据源）
    系统提示词的权威科室名单           ─┘

**工具层零改动**：只要组合根把 ``doctor_service`` 换成 ``HisDoctorService``，
``BookAppointmentTool`` / ``QuerySlotTool`` / ``QueryDoctorsTool`` /
``load_system_prompt()`` 全部原样工作。

**接真实 HIS 时改哪里**：只改 ``his_client.py`` 的 ``_paths`` 端点映射与
``_FIELD_MAP`` 字段映射（或由组合根注入 ``paths=``），本类不用动；
院方字典字段命名差异由 ``_dept_name()`` / ``_doctor_info()`` 两处吸收。

**空名单必须 fail-fast**：启动预加载时若 HIS 返回空科室清单，绝不能静默
降级成「本院没有科室」—— 真机踩过：模型拿不到权威名单就会编造科室名
（用户问「内科」，模型回「呼吸科」）。宁可拒绝启动，也不能给假名单。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

from xiaozhi.service.doctor_info import DoctorInfo
from xiaozhi.service.doctor_service import DoctorService
from xiaozhi.service.his_client import HisClient, HisError

logger = logging.getLogger(__name__)


class HisCatalogUnavailable(RuntimeError):
    """HIS 院方主档不可用（连不上 / 返回结构异常 / 空名单）。

    组合根应把它转成启动失败，而不是降级成空目录。
    """


#: 院方字典字段名 → 本项目标准字段名。接真实 HIS 时**只改这张表**。
_FIELD_MAP: Dict[str, tuple] = {
    "dept": ("deptName", "dept_name", "name", "departmentName"),
    "deptCode": ("deptCode", "dept_code", "code"),
    "doctor": ("doctorName", "doctor_name", "name", "empName"),
    "title": ("title", "professionalTitle", "doctorTitle", "positionalTitle"),
    "expertise": ("expertise", "specialty", "goodAt", "skills"),
    "status": ("status", "delFlag", "enabled"),
}


def _pick(row: Dict[str, Any], key: str, default: str = "") -> str:
    for field in _FIELD_MAP[key]:
        value = row.get(field)
        if value not in (None, ""):
            return str(value)
    return default


def _dept_name(row: Dict[str, Any]) -> str:
    return _pick(row, "dept").strip()


def _doctor_info(row: Dict[str, Any], department: str = "") -> DoctorInfo:
    return DoctorInfo(
        name=_pick(row, "doctor").strip(),
        department=(department or _dept_name(row)).strip(),
        title=_pick(row, "title").strip(),
        expertise=_pick(row, "expertise").strip(),
    )


class HisDoctorService(DoctorService):
    """从 HIS 院方主档读取科室 / 医生名录（带缓存）。

    * 启动时 ``refresh()`` 一次性拉取全量字典并校验非空；
    * 运行期只读缓存 —— 字典是准静态数据，不该让每轮对话都打一次 HIS；
    * HIS 短时抖动时沿用上次成功快照并告警，**不返回空名单**；
    * ``refresh()`` 可显式调用（院方字典更新后运维手动刷新）。
    """

    def __init__(self, client: HisClient, *, org_id: str = "") -> None:
        self._client = client
        self._org_id = org_id
        self._lock = threading.RLock()
        self._departments: List[str] = []
        self._doctors: Dict[str, List[DoctorInfo]] = {}
        self._loaded = False

    # ---------- 启动预加载 ----------

    def refresh(self) -> Dict[str, Any]:
        """拉取全量院方主档；失败或空名单抛 ``HisCatalogUnavailable``。"""
        try:
            dept_rows = self._client.list_departments(org_id=self._org_id)
            doctor_rows = self._client.list_doctors()
        except HisError as exc:
            raise HisCatalogUnavailable(
                f"HIS 院方主档不可用：{exc}（detail={exc.detail[:160]}）"
            ) from exc
        except Exception as exc:  # 传输层未包装异常
            raise HisCatalogUnavailable(f"HIS 院方主档不可用：{exc}") from exc

        departments: List[str] = []
        # 停用过滤由 **HIS 侧**负责（沙箱 SQL 已 WHERE status='0'）。
        # 适配层不猜院方 status 编码：有的 HIS 0=正常 1=停用，有的反过来，
        # 在这里误判会把正常科室说成"本院没这个科室"，直接诱发模型编科室名。
        for row in dept_rows:
            name = _dept_name(row)
            if name:
                departments.append(name)
        doctors: Dict[str, List[DoctorInfo]] = {}
        for row in doctor_rows:
            dept = _dept_name(row)
            info = _doctor_info(row)
            if not info.name or not dept:
                continue
            bucket = doctors.setdefault(dept, [])
            if not any(d.name == info.name for d in bucket):
                bucket.append(info)

        # 空名单 = 主档没灌好 / 字段映射对不上 → 不能静默放行
        if not departments:
            raise HisCatalogUnavailable(
                "HIS 科室字典为空：请检查 HIS 主档是否已灌库，以及 "
                "his_client._FIELD_MAP 的 dept 字段映射是否匹配院方字段名。"
            )
        if not doctors:
            raise HisCatalogUnavailable(
                "HIS 医生名录为空：请检查 HIS 主档与 doctor 字段映射。"
            )
        # 字典里出现但名录里没有的科室：允许（科室可能暂无出诊专家），
        # 但要在日志里可见，避免"能查到科室、查不到任何医生"被当成正常。
        missing = [d for d in departments if d not in doctors]
        if missing:
            logger.warning("HIS 主档：以下科室没有医生记录：%s", "、".join(missing))

        with self._lock:
            self._departments = departments
            self._doctors = doctors
            self._loaded = True
        logger.info(
            "HIS 院方主档已加载：%d 个科室 / %d 位医生（数据源：HIS REST）",
            len(departments), sum(len(v) for v in doctors.values()),
        )
        return {
            "departments": len(departments),
            "doctors": sum(len(v) for v in doctors.values()),
        }

    def ensure_loaded(self) -> None:
        """组合根启动时调用：必须成功，否则拒绝启动。"""
        if not self._loaded:
            self.refresh()

    # ---------- DoctorService（工具层依赖的唯一接口） ----------

    def list_departments(self) -> List[str]:
        self._ensure()
        with self._lock:
            return list(self._departments)

    def list_doctors(self, department: str) -> List[DoctorInfo]:
        if not department or not department.strip():
            return []
        self._ensure()
        key = self._match_department(department.strip())
        with self._lock:
            return list(self._doctors.get(key, [])) if key else []

    def find_doctor(self, department: str, doctor_name: str) -> Optional[DoctorInfo]:
        if not doctor_name or not doctor_name.strip():
            return None
        name = doctor_name.strip()
        for doctor in self.list_doctors(department):
            if doctor.name == name or name in doctor.name or doctor.name in name:
                return doctor
        return None

    # ---------- 内部 ----------

    def _ensure(self) -> None:
        """运行期：已有快照直接用；没有则尝试拉一次，失败沿用旧快照。"""
        if self._loaded:
            return
        try:
            self.refresh()
        except HisCatalogUnavailable as exc:
            # 组合根已强制 ensure_loaded()，走到这里说明是运行期刷新失败
            # 且尚无快照 —— 抛错，不伪装成"本院没有科室"。
            raise HisCatalogUnavailable(
                f"HIS 院方主档尚未就绪，拒绝返回空名录：{exc}"
            ) from exc

    def _match_department(self, department: str) -> Optional[str]:
        """与旧实现一致的科室名归一（去掉「专家门诊」等后缀再匹配）。"""
        with self._lock:
            names = list(self._departments)
        if department in names:
            return department
        stripped = (
            department.replace("专家门诊", "").replace("普通门诊", "").replace("门诊", "")
        )
        if stripped in names:
            return stripped
        for name in names:
            if department.startswith(name) and len(name) * 2 >= len(department):
                return name
        return None

    def refresh_best_effort(self) -> bool:
        """院方字典更新后的手动刷新：失败保留旧快照并返回 False。"""
        try:
            self.refresh()
            return True
        except HisCatalogUnavailable as exc:
            logger.warning("HIS 院方主档刷新失败，沿用上次快照：%s", exc)
            return False


__all__ = ["HisCatalogUnavailable", "HisDoctorService"]
