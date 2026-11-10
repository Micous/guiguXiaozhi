"""专家名录接口 —— 对接医院科室/医生主数据的扩展点。

默认实现是**真实检索**（查本地向量库 Milvus），不是 mock。
如果院方后续提供医生主数据接口，新增实现并替换 Bean 装配即可。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from xiaozhi.service.doctor_info import DoctorInfo


class DoctorService(ABC):
    """专家名录服务"""

    @abstractmethod
    def list_departments(self) -> List[str]:
        """全部科室名"""

    @abstractmethod
    def list_doctors(self, department: str) -> List[DoctorInfo]:
        """某科室的专家列表"""

    @abstractmethod
    def find_doctor(self, department: str, doctor_name: str) -> Optional[DoctorInfo]:
        """在某科室内查找指定医生"""
