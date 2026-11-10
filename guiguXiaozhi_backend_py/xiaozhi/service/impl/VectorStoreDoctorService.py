"""专家名录服务 —— **直接检索本地向量库（Milvus）**，不是 mock。

专家名单来自知识库 doctors.md，启动时已被切分、向量化写入 Milvus；
这里通过向量检索把该文件的段落召回并解析成「科室 → 医生列表」。

扩展点：若院方提供医生主数据接口，新增 HisDoctorService implements DoctorService
并在 application.py 中替换 Bean 即可，工具层代码不用动。
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

from xiaozhi.service.doctor_info import DoctorInfo
from xiaozhi.service.doctor_service import DoctorService


class VectorStoreDoctorService(DoctorService):
    """基于向量库的专家名录（真实检索）"""

    def __init__(self, retrieve_all_chunks, roster_file: str = "doctors.md") -> None:
        """
        Args:
            retrieve_all_chunks: 无参可调用，返回 [(id, text, metadata), ...]
            roster_file:        专家名单所在的知识库文件名
        """
        self._retrieve_all_chunks = retrieve_all_chunks
        self._roster_file = roster_file
        self._lock = threading.Lock()
        self._roster: Dict[str, List[DoctorInfo]] = {}
        self._loaded = False

    # ---------- DoctorService ----------

    def list_departments(self) -> List[str]:
        self._ensure_loaded()
        return list(self._roster.keys())

    def list_doctors(self, department: str) -> List[DoctorInfo]:
        if not department or not department.strip():
            return []
        self._ensure_loaded()
        key = self._match_department(department.strip())
        return list(self._roster.get(key, [])) if key else []

    def find_doctor(self, department: str, doctor_name: str) -> Optional[DoctorInfo]:
        if not doctor_name or not doctor_name.strip():
            return None
        name = doctor_name.strip()
        for doctor in self.list_doctors(department):
            if doctor.name == name or name in doctor.name or doctor.name in name:
                return doctor
        return None

    # ---------- 内部 ----------

    @staticmethod
    def _department_from_path(heading_path: str) -> Optional[str]:
        """从 "文档标题 / 科室名" 里取出最后一段作为科室名"""
        if not heading_path:
            return None
        parts = [p.strip() for p in heading_path.split("/") if p.strip()]
        return parts[-1] if parts else None

    def _match_department(self, department: str) -> Optional[str]:
        for key in self._roster:
            if key == department:
                return key
        stripped = department.replace("专家门诊", "").replace("普通门诊", "").replace("门诊", "")
        for key in self._roster:
            if key == stripped:
                return key
        for key in self._roster:
            if department.startswith(key) and len(key) * 2 >= len(department):
                return key
        return None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                for _id, text, metadata in self._retrieve_all_chunks():
                    if metadata.get("file_name") != self._roster_file:
                        continue
                    self._parse(text, metadata)
                self._loaded = True
            except Exception:
                # 检索不可用时保持空目录，工具会返回"暂无专家信息"，不阻断主流程
                self._loaded = True

    def _parse(self, text: str, metadata: Dict[str, str]) -> None:
        """解析一个块

        科室名从**元数据**取（heading_path / section），而不是从正文里找 "## 标题" ——
        分块时标题已经折叠成块首的 "# 路径" 前缀，正文里不再有 "## 科室" 这一行。
        """
        current = (metadata or {}).get("section") or self._department_from_path(
            (metadata or {}).get("heading_path", "")
        )
        if not current:
            return
        self._roster.setdefault(current, [])

        for line in text.split("\n"):
            trimmed = line.strip()
            if trimmed.startswith("# "):
                continue  # 块首的标题路径前缀
            if not trimmed.startswith("- "):
                continue
            body = trimmed[2:].strip()
            parts = body.split("，")
            if len(parts) < 3:
                continue
            name = parts[0].strip()
            title = parts[2].strip()
            expertise = ""
            marker = body.find("擅长：")
            if marker >= 0:
                expertise = body[marker + 3 :].strip()
            if not any(d.name == name for d in self._roster[current]):
                self._roster[current].append(DoctorInfo(name, current, title, expertise))
