"""工具入参归一化。

**为什么需要这一层**：Ollama 把工具 schema 交给模型后，模型生成 tool_call 时
参数键名**不一定是 schema 里的属性名** —— 实测同一个工具，模型时而生成
``arg0/arg1/...``，时而生成 schema 里的**中文描述名**（"患者姓名"/"身份证号"…）。
直接照单全收会导致参数全空、工具形同虚设。

策略：把任意形态的键名映射到**规范名**（username/id_card/…），并保留
位置参数（arg0..argN）作为兜底。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 规范名 -> 可接受的别名（中文描述名、常见同义写法、位置参数）
_ALIASES: Dict[str, List[str]] = {
    "username": ["arg0", "患者姓名", "姓名", "患者", "user_name", "name"],
    "id_card": ["arg1", "身份证号", "身份证", "证件号", "idcard", "id_number"],
    "department": ["arg2", "科室", "预约科室", "就诊科室", "department_name", "dept"],
    "date": ["arg3", "日期", "预约日期", "就诊日期", "appointment_date", "date_str"],
    "time": ["arg4", "时段", "预约时段", "时间", "就诊时段", "time_slot", "period"],
    "doctor_name": [
        "arg5",
        "医生",
        "医生姓名",
        "预约医生",
        "出诊医生",
        "doctor",
        "doctorName",
    ],
    "keyword": ["arg1", "关键词", "擅长方向", "keyword", "expertise"],
}

# 反查表：别名 -> 规范名
_ALIAS_TO_CANONICAL: Dict[str, str] = {}
for _canonical, _aliases in _ALIASES.items():
    _ALIAS_TO_CANONICAL[_canonical] = _canonical
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL.setdefault(_alias, _canonical)


def _coerce_scalar(value: Any) -> Any:
    """值级自愈：修小模型的常见参数形态错误（事故 2026-11-10）。

    * 嵌套单键 dict（``{"date": "2026-11-11"}``）→ 解包出字符串；
    * int/float（``20261111``）→ 转字符串（工具层全部按字符串处理）；
    * 其余（list、多键 dict 等）原样保留，交给上层校验报错。
    """
    if isinstance(value, dict):
        if len(value) == 1:
            inner = next(iter(value.values()))
            if isinstance(inner, str):
                return _coerce_scalar(inner)
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return value


def normalize_arguments(
    args: Dict[str, Any],
    canonical_order: List[str],
) -> Dict[str, Any]:
    """把工具入参归一化到规范名。

    Args:
        args:            模型给的原始入参
        canonical_order: 规范名顺序，用于位置参数（arg0/arg1/…）的兜底映射
    """
    if not isinstance(args, dict):
        return {}

    normalized: Dict[str, Any] = {}
    leftovers: List[Any] = []

    for key, value in args.items():
        canonical = _ALIAS_TO_CANONICAL.get(str(key).strip())
        if canonical and canonical not in normalized:
            normalized[canonical] = _coerce_scalar(value)
        else:
            leftovers.append(value)

    # 位置参数兜底：arg0/arg1/… 没被识别时按顺序填入
    positional = [
        value
        for key, value in args.items()
        if str(key).strip() in _ALIAS_TO_CANONICAL
        and _ALIAS_TO_CANONICAL[str(key).strip()] not in normalized
    ]
    if positional:
        for canonical, value in zip(canonical_order, positional):
            if canonical not in normalized:
                normalized[canonical] = value

    return normalized
