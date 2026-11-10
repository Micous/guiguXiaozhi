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
