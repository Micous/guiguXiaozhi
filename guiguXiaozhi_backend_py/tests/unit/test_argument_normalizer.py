"""工具入参归一化单测（2026-11-10 事故回归）。

真实事故：模型把 date 传成嵌套 dict ``{"date": "2026-11-11"}``，
工具层直接拒绝 → 模型转头谎报"预约已经成功"。
现在归一化层先自愈，能修的修好，修不了的才报错。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.tools.argument_normalizer import normalize_arguments  # noqa: E402


def test_nested_single_key_dict_is_unwrapped():
    args = normalize_arguments(
        {"date": {"date": "2026-11-11"}, "department": "内科"},
        ["username", "id_card", "department", "date", "time", "doctor_name"],
    )
    assert args["date"] == "2026-11-11"
    assert args["department"] == "内科"


def test_numeric_values_become_strings():
    args = normalize_arguments({"date": 20261111}, ["date"])
    assert args["date"] == "20261111"


def test_multi_key_dict_and_list_are_left_alone():
    args = normalize_arguments(
        {"date": {"inner": "a", "other": "b"}, "time": ["上午"]},
        ["date", "time"],
    )
    assert args["date"] == {"inner": "a", "other": "b"}
    assert args["time"] == ["上午"]


def test_alias_keys_still_map_and_coerce():
    args = normalize_arguments(
        {"预约日期": {"date": "2026-11-20"}, "身份证号": 110101199005054322},
        ["username", "id_card", "department", "date", "time"],
    )
    assert args["date"] == "2026-11-20"
    assert args["id_card"] == "110101199005054322"


# ---- schema 驱动的新版归一化 + 校验（核心模块第 2 批） ----

from xiaozhi.core.tool import RiskLevel, ToolDefinition, ToolParameter  # noqa: E402
from xiaozhi.tools.argument_normalizer import (  # noqa: E402
    normalize_arguments_by_schema,
    validate_arguments,
)


def _booking_definition() -> ToolDefinition:
    return ToolDefinition(
        name="预约挂号",
        description="提交预约",
        parameters=[
            ToolParameter("username", "string", "患者姓名"),
            ToolParameter("id_card", "string", "患者身份证号"),
            ToolParameter("department", "string", "预约科室"),
            ToolParameter("date", "string", "预约日期"),
            ToolParameter("time", "string", "预约时段"),
            ToolParameter("doctor_name", "string", "预约医生，可选", required=False),
        ],
        risk_level=RiskLevel.WRITE,
    )


def test_schema_normalization_maps_chinese_description_alias():
    """参数的中文描述名（"患者姓名"）自动映射到规范名 —— 不再依赖手写别名表"""
    args = normalize_arguments_by_schema(
        {"患者姓名": "王新勃", "患者身份证号": "110101199005054322"},
        _booking_definition(),
    )
    assert args["username"] == "王新勃"
    assert args["id_card"] == "110101199005054322"


def test_schema_normalization_falls_back_to_domain_alias_table():
    """schema 描述之外的中文别名（"身份证号"）仍由领域别名表兜底"""
    args = normalize_arguments_by_schema(
        {"身份证号": "110101199005054322", "日期": "2026-11-20"},
        _booking_definition(),
    )
    assert args["id_card"] == "110101199005054322"
    assert args["date"] == "2026-11-20"


def test_schema_normalization_maps_camel_and_plain_variants():
    args = normalize_arguments_by_schema(
        {"idCard": "110101199005054322", "doctorName": "王笠", "idcard": "x"},
        _booking_definition(),
    )
    assert args["id_card"] == "110101199005054322"
    assert args["doctor_name"] == "王笠"


def test_schema_normalization_positional_fallback():
    """完全未识别的键按 schema 参数顺序兜底（覆盖 arg0/arg1/… 形态）"""
    args = normalize_arguments_by_schema(
        {"arg0": "王新勃", "arg1": "110101199005054322"},
        _booking_definition(),
    )
    assert args["username"] == "王新勃"
    assert args["id_card"] == "110101199005054322"


def test_schema_normalization_handles_non_dict():
    assert normalize_arguments_by_schema("garbage", _booking_definition()) == {}


def test_validate_arguments_missing_required():
    definition = _booking_definition()
    error = validate_arguments({"username": "王新勃"}, definition)
    assert error is not None
    assert "身份证" in error  # 用中文描述指认缺的是哪个参数
    assert "重新调用" in error or "确认" in error


def test_validate_arguments_optional_missing_is_ok():
    error = validate_arguments(
        {
            "username": "王新勃",
            "id_card": "110101199005054322",
            "department": "神经内科",
            "date": "2026-11-20",
            "time": "上午",
        },
        _booking_definition(),
    )
    assert error is None  # doctor_name 可选，缺失不报错


def test_validate_arguments_type_mismatch():
    definition = _booking_definition()
    args = {p.name: "x" for p in definition.parameters if p.required}
    args["date"] = ["2026-11-20"]  # list 冒充 string
    error = validate_arguments(args, definition)
    assert error is not None and "string" in error


def test_validate_arguments_enum_violation():
    definition = ToolDefinition(
        name="t",
        description="d",
        parameters=[ToolParameter("time", "string", "时段", enum=["上午", "下午"])],
    )
    error = validate_arguments({"time": "中午"}, definition)
    assert error is not None and "上午/下午" in error
    assert validate_arguments({"time": "上午"}, definition) is None
