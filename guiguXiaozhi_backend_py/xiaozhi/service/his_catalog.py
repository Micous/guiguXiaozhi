"""HIS 院方主档（科室字典 / 医生名录）——**院方数据的唯一定义处**。

这一层属于 **HIS 域**，不属于应用域。它只在 HIS 服务启动时被读取一次，
灌进 HIS 自己的主档表（``sys_dept`` / ``sys_doctor``）之后，运行时
**应用侧只能通过 HIS 的 REST 接口查询**，不得直接 import 本模块。

为什么要独立出来
----------------

改造前院方数据有两份、方向还是反的：

.. code-block:: text

    doctors.md ──> VectorStoreDoctorService（应用侧，Milvus 检索）
                        │
                        └──> seed_from_roster() ──> 本地 HIS 排班

于是「查询科室/查询医生」读知识库，「查号源/预约」读 HIS，两边可能不一致；
将来接真实医院 HIS 时，工具层、提示词、名录服务都要跟着改。

改造后：

.. code-block:: text

    doctors.md（院方导入源文件，一次性）
        └──> HIS 主档表 sys_dept / sys_doctor / clinic_index
                  └──> REST /dept/list、/doctor/list、/clinicForRegist/list
                            └──> HisClient ──> HisDoctorService + HisSlotService
                                      └──> 工具层（零改动）

真实医院接入时，HIS 侧主档由院方数据库提供，本模块只作为**本地模拟 HIS
的种子数据加载器**被替换；应用侧适配层（``his_client.py``）只改端点与
字段映射，其他代码不动。

字段说明
--------

* ``dept_code``：科室编码（真实 HIS 是院方编码体系，这里按字典序生成 D001…）
* ``doctor_id``：医生工号（真实 HIS 来自人事主档）
* ``title`` / ``expertise``：职称与擅长方向，供「查询科室专家」工具展示
* ``status``：``0`` 正常 / ``1`` 停诊（字典接口只返回 ``0``）
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

#: 院方导入源文件。真实 HIS 由院方主档表提供，这里用知识库文件做种子。
BACKEND_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG_PATH = BACKEND_ROOT / "xiaozhi" / "knowledge" / "doctors.md"


@dataclass(frozen=True)
class DepartmentRecord:
    """科室字典一行（对齐 HIS ``sys_dept``）。"""

    dept_id: str
    dept_code: str
    dept_name: str
    org_id: str = ""
    status: str = "0"

    def as_row(self) -> Dict[str, Any]:
        return {
            "deptId": self.dept_id,
            "deptCode": self.dept_code,
            "deptName": self.dept_name,
            "orgId": self.org_id,
            "status": self.status,
        }


@dataclass(frozen=True)
class DoctorRecord:
    """医生主档一行（对齐 HIS ``sys_doctor``）。"""

    doctor_id: str
    dept_code: str
    dept_name: str
    name: str
    gender: str = ""
    title: str = ""
    expertise: str = ""
    status: str = "0"

    def as_row(self) -> Dict[str, Any]:
        return {
            "doctorId": self.doctor_id,
            "deptCode": self.dept_code,
            "deptName": self.dept_name,
            "doctorName": self.name,
            "gender": self.gender,
            "title": self.title,
            "expertise": self.expertise,
            "status": self.status,
        }

    def as_doctor_info(self) -> Dict[str, Any]:
        """转成应用侧 ``DoctorInfo`` 的构造参数（适配层负责最终映射）。"""
        return {
            "name": self.name,
            "department": self.dept_name,
            "title": self.title,
            "expertise": self.expertise,
        }


@dataclass(frozen=True)
class HisCatalog:
    """一份院方主档快照。"""

    org_id: str
    departments: Tuple[DepartmentRecord, ...]
    doctors: Tuple[DoctorRecord, ...]

    def dept_names(self) -> List[str]:
        return [d.dept_name for d in self.departments]

    def dept_rows(self) -> List[Dict[str, Any]]:
        return [d.as_row() for d in self.departments]

    def doctor_rows(self, dept_name: str = "") -> List[Dict[str, Any]]:
        return [
            d.as_row()
            for d in self.doctors
            if not dept_name or d.dept_name == dept_name
        ]

    def roster(self) -> List[Tuple[str, str]]:
        """``(科室, 医生)`` 列表 —— 供 HIS 生成排班索引。"""
        return [(d.dept_name, d.name) for d in self.doctors]


def _parse_line(line: str) -> Tuple[str, str, str, str]:
    """解析一行 ``- 姓名，性别，职称，科室。擅长：...``。"""
    body = line[2:].strip()
    head, _, tail = body.partition("。")
    parts = [p.strip() for p in head.replace(",", "，").split("，") if p.strip()]
    name = parts[0] if parts else ""
    gender = parts[1] if len(parts) > 1 else ""
    title = parts[2] if len(parts) > 2 else ""
    expertise = ""
    marker = tail.find("擅长：")
    if marker >= 0:
        expertise = tail[marker + 3 :].strip().rstrip("。;； ")
    return name, gender, title, expertise


def load_his_catalog(
    path: Path | None = None, *, org_id: str = ""
) -> HisCatalog:
    """从院方导入文件解析主档。

    文件带 YAML frontmatter（知识溯源元数据），解析时必须跳过，否则会把
    ``source:`` / ``reviewer:`` 之类的行当成科室或医生。

    文件缺失时不静默返回空目录 —— 空目录会让「查询科室」工具把本院说成
    "没有任何科室"，模型随即编造科室名。这里退化为一组最小可用主档，
    并让调用方在启动日志里看得见。
    """
    target = Path(path) if path else DEFAULT_CATALOG_PATH
    pairs: List[Tuple[str, str, str, str, str]] = []  # dept,name,gender,title,expertise
    dept = ""
    in_frontmatter = False
    if target.exists():
        for raw in target.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line == "---":
                in_frontmatter = not in_frontmatter
                continue
            if in_frontmatter:
                continue
            if line.startswith("## "):
                dept = line[3:].strip()
            elif line.startswith("- ") and dept:
                name, gender, title, expertise = _parse_line(line)
                if name:
                    pairs.append((dept, name, gender, title, expertise))

    if not pairs:
        pairs = [
            ("呼吸内科", "徐国兴", "男", "主任医师", "慢性阻塞性肺疾病、支气管哮喘"),
            ("心内科", "张抒扬", "女", "主任医师", "冠心病、心力衰竭"),
        ]

    dept_names = sorted({p[0] for p in pairs})
    dept_codes = {name: f"D{i:03d}" for i, name in enumerate(dept_names, start=1)}
    departments = tuple(
        DepartmentRecord(
            dept_id=f"dept-{dept_codes[name]}",
            dept_code=dept_codes[name],
            dept_name=name,
            org_id=org_id,
        )
        for name in dept_names
    )
    doctors = tuple(
        DoctorRecord(
            doctor_id=f"doc-{i:04d}",
            dept_code=dept_codes[dept],
            dept_name=dept,
            name=name,
            gender=gender,
            title=title,
            expertise=expertise,
        )
        for i, (dept, name, gender, title, expertise) in enumerate(pairs, start=1)
    )
    return HisCatalog(org_id=org_id, departments=departments, doctors=doctors)


def catalog_stats(catalog: HisCatalog) -> Dict[str, Any]:
    """启动日志用：一眼看出主档是否完整加载。"""
    return {
        "departments": len(catalog.departments),
        "doctors": len(catalog.doctors),
        "withTitle": sum(1 for d in catalog.doctors if d.title),
        "withExpertise": sum(1 for d in catalog.doctors if d.expertise),
    }


__all__ = [
    "DepartmentRecord",
    "DoctorRecord",
    "HisCatalog",
    "catalog_stats",
    "load_his_catalog",
]

# asdict 只在调试导出时用，保留引用避免静态检查报未使用
_ = asdict
