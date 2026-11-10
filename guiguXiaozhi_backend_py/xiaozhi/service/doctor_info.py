"""医生信息 —— 与检索层解耦的纯数据对象。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DoctorInfo:
    """医生信息"""

    name: str
    department: str
    title: str
    expertise: str = ""

    def brief(self) -> str:
        """简短展示，例如「王笠（神经内科 主任医师）」"""
        return f"{self.name}（{self.department} {self.title}）"

    def describe(self) -> str:
        return f"{self.brief()}，擅长：{self.expertise}"
