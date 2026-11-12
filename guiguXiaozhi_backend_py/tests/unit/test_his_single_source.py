"""院方数据单一源（HIS）护栏测试 —— 锁住「所有院方数据只在 HIS」。

这些是用例是行为契约，不是实现细节：

1. 院方主档解析：frontmatter 不能混进科室、职称/擅长必须解析出来；
2. ``HisDoctorService`` 实现既有 ``DoctorService`` 抽象，工具层零改动；
3. 空科室字典必须 fail-fast（静默空名单会让模型编造科室名）；
4. 院方字典字段命名差异由 ``_FIELD_MAP`` 吸收（接真 HIS 只改映射表）；
5. HIS 建档签发的患者引用不会被本地派生值顶掉；
6. 端点映射集中在 ``HisClient._paths``，不在业务代码里硬编码。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# 本机沙箱不允许 pytest 的 tmp_path fixture 创建目录，改用模块内临时文件
_TMP_CATALOG = Path("/tmp/xiaozhi_his_catalog_test.md")

from xiaozhi.service.doctor_service import DoctorService
from xiaozhi.service.his_catalog import catalog_stats, load_his_catalog
from xiaozhi.service.his_client import HisClient
from xiaozhi.service.his_doctor_service import (
    HisCatalogUnavailable,
    HisDoctorService,
    _dept_name,
    _doctor_info,
)
from xiaozhi.service.local_his import LocalHisServer


# ---------------------------------------------------------------- 主档解析


def test_catalog_parses_depts_titles_expertise_and_skips_frontmatter():
    md = _TMP_CATALOG
    md.write_text(
        "---\nsource: 院方官网\nreviewer: 医务处\n---\n\n"
        "## 呼吸内科\n"
        "- 徐国兴，男，主任医师，呼吸内科。擅长：哮喘、慢阻肺。\n"
        "## 心内科\n"
        "- 张抒扬，女，主任医师，心内科。擅长：冠心病。\n",
        encoding="utf-8",
    )
    catalog = load_his_catalog(md, org_id="ORG-X")
    assert catalog.dept_names() == sorted(["心内科", "呼吸内科"])
    assert len(catalog.doctors) == 2
    assert all(d.title == "主任医师" for d in catalog.doctors)
    # 擅长字段尾部的句号要去掉
    assert all(not d.expertise.endswith("。") for d in catalog.doctors)
    assert "哮喘" in next(d.expertise for d in catalog.doctors if d.name == "徐国兴")
    # frontmatter 里的 source/reviewer 绝不能被当成科室或医生
    assert not any("source" in d.dept_name.lower() for d in catalog.departments)
    stats = catalog_stats(catalog)
    assert stats["withTitle"] == 2 and stats["withExpertise"] == 2


def test_catalog_dept_codes_are_stable_and_unique():
    a = load_his_catalog()
    b = load_his_catalog()
    assert [d.dept_code for d in a.departments] == [d.dept_code for d in b.departments]
    assert len({d.dept_code for d in a.departments}) == len(a.departments)
    assert len({d.doctor_id for d in a.doctors}) == len(a.doctors)
    # 每个医生的科室编码必须能指回科室字典
    codes = {d.dept_code for d in a.departments}
    assert all(d.dept_code in codes for d in a.doctors)


# ---------------------------------------------------------------- 字典适配层


def test_his_doctor_service_implements_existing_abstraction():
    """工具层依赖的抽象不变 —— 这是「工具层零改动」的契约。"""
    assert issubclass(HisDoctorService, DoctorService)


def test_empty_catalog_fails_fast_instead_of_silent_empty():
    """空科室字典不能静默降级：模型拿不到权威名单就会编造科室名。"""
    server = LocalHisServer(catalog=load_his_catalog(), limit_num=2)
    url = server.start()
    try:
        client = HisClient(url, token="t", timeout=2.0)
        svc = HisDoctorService(client)
        # 用不存在的 HIS 地址模拟主档不可用
        broken = HisDoctorService(HisClient("http://127.0.0.1:1", timeout=0.2))
        with pytest.raises(HisCatalogUnavailable):
            broken.ensure_loaded()
        # 正常服务仍能加载
        assert len(svc.list_departments()) >= 10
    finally:
        server.stop()


def test_empty_dept_list_raises():
    """HIS 返回空数组 → fail-fast，不能给一份假名单。"""

    class _EmptyClient:
        def list_departments(self, *, org_id=""):
            return []

        def list_doctors(self, dept_name="", *, keyword=""):
            return []

    with pytest.raises(HisCatalogUnavailable, match="科室字典为空"):
        HisDoctorService(_EmptyClient()).refresh()


def test_field_map_absorbs_vendor_naming():
    """院方字段名不同时只改 _FIELD_MAP，不改工具层。"""
    row = {
        "departmentName": "神经内科",
        "code": "D010",
    }
    assert _dept_name(row) == "神经内科"
    doctor = _doctor_info(
        {
            "empName": "王笠",
            "professionalTitle": "主任医师",
            "specialty": "脑血管病",
        },
        department="神经内科",
    )
    assert doctor.name == "王笠"
    assert doctor.title == "主任医师"
    assert doctor.expertise == "脑血管病"
    assert doctor.department == "神经内科"


def test_doctor_service_department_normalization_and_keyword():
    server = LocalHisServer(catalog=load_his_catalog(), limit_num=2)
    url = server.start()
    try:
        svc = HisDoctorService(HisClient(url, token="t", timeout=2.0))
        svc.ensure_loaded()
        # 「呼吸内科专家门诊」应归一到「呼吸内科」（与旧实现一致）
        assert svc.list_doctors("呼吸内科专家门诊")
        assert svc.find_doctor("呼吸内科", "徐国兴") is not None
        assert svc.find_doctor("呼吸内科", "不存在的人") is None
        assert svc.list_doctors("") == []
    finally:
        server.stop()


# ---------------------------------------------------------------- 端点集中


def test_all_hospital_endpoints_are_centralized():
    """所有院方端点都在 HisClient._paths —— 接真 HIS 只改这一张表。"""
    client = HisClient("http://127.0.0.1:1", timeout=0.2)
    required = {
        "slots", "slot", "hold", "confirm", "release", "cancel",
        "booking", "byKey", "bookings", "patient", "patients",
        "depts", "dept", "doctors",
    }
    assert all(client.path(key) for key in required)
    # 覆盖端点不需要改业务代码
    client.configure_paths(depts="/custom/dept/list", doctors="/custom/doctor/list")
    assert client.path("depts") == "/custom/dept/list"
    assert client.path("doctors") == "/custom/doctor/list"


def test_his_server_exposes_catalog_and_patient_master_endpoints():
    """HIS 自持主档：科室字典 / 医生名录 / 患者建档都从 HIS 读。"""
    server = LocalHisServer(catalog=load_his_catalog(), limit_num=2)
    url = server.start()
    try:
        client = HisClient(url, token="t", timeout=2.0)
        depts = client.list_departments()
        doctors = client.list_doctors("呼吸内科")
        assert len(depts) >= 10
        assert doctors and all(d.get("title") for d in doctors)
        # 字典医生 ⊆ 号源医生（同源）
        his_doctors = {d["doctorName"] for d in doctors}
        slot_doctors = {
            s.doctor_name
            for s in client.query_slots({"deptName": "呼吸内科"})
            if s.doctor_name
        }
        assert slot_doctors <= his_doctors
        # 患者主档由 HIS 签发
        created = client.create_patient(
            {"idNo": "610121199401032831", "name": "张三"},
            idempotency_key="pat-1",
        )
        assert created["patientRef"].startswith("PAT-")
        rows = client.request(
            "GET", "/patMasterIndex/list", params={"idNo": "610121199401032831"}
        ).data
        assert any(r["patientRef"] == created["patientRef"] for r in rows)
    finally:
        server.stop()
