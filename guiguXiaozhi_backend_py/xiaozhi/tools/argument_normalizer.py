"""工具入参归一化 + schema 校验。

**为什么需要这一层**：Ollama 把工具 schema 交给模型后，模型生成 tool_call 时
参数键名**不一定是 schema 里的属性名** —— 实测同一个工具，模型时而生成
``arg0/arg1/...``，时而生成 schema 里的**中文描述名**（"患者姓名"/"身份证号"…）。
直接照单全收会导致参数全空、工具形同虚设。

两层归一化入口：
  * ``normalize_arguments``          —— 旧版：领域别名表（挂号域专用，保留兼容，
                                        工具内部已在用，行为幂等不变）；
  * ``normalize_arguments_by_schema``—— 新版（推荐）：别名从 ``ToolDefinition``
                                        自动生成（参数名 / snake↔camel 变体 /
                                        纯中文短描述），领域别名表降级为兜底。
                                        旧表存在键冲突（``arg1`` 同时映射
                                        id_card/keyword），新入口优先走 schema。

``validate_arguments`` 按 schema 做 required / type / enum 三检，
由 SecureToolExecutor 在 L2 统一调用。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # 避免运行时循环导入
    from xiaozhi.core.tool import ToolDefinition

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


# ---------------------------------------------------------------------------
# schema 驱动的新版归一化 + 校验
# ---------------------------------------------------------------------------

# 纯中文短描述可作别名（"患者姓名"）；带标点/ ASCII 的长描述不行
_SHORT_CJK_RE = re.compile(r"[\u4e00-\u9fa5]{2,6}")

# JSON Schema 类型 -> Python 类型检查
_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def _schema_alias_map(definition: "ToolDefinition") -> Dict[str, str]:
    """从 ToolDefinition 自动生成别名映射。

    每个参数生成：参数名本身、无下划线变体（id_card→idcard）、
    camelCase 变体（id_card→idCard）、以及**纯中文短描述**（"患者姓名"）。
    """
    alias_map: Dict[str, str] = {}
    for param in definition.parameters:
        candidates = {param.name, param.name.replace("_", "")}
        camel = "".join(
            part[:1].upper() + part[1:] for part in param.name.split("_") if part
        )
        candidates.add(camel)
        candidates.add(camel[:1].lower() + camel[1:])
        description = (param.description or "").strip()
        if _SHORT_CJK_RE.fullmatch(description):
            candidates.add(description)
        for candidate in candidates:
            alias_map.setdefault(candidate, param.name)
    return alias_map


def normalize_arguments_by_schema(
    args: Dict[str, Any],
    definition: "ToolDefinition",
) -> Dict[str, Any]:
    """schema 驱动的归一化（推荐入口，SecureToolExecutor L2 统一调用）。

    别名解析优先级：**schema 自动生成 > 领域别名表（legacy 兜底） > 位置兜底**。
    未识别键的值按 schema 参数顺序做位置兜底（覆盖 arg0/arg1/… 的形态）。
    """
    if not isinstance(args, dict):
        return {}
    schema_map = _schema_alias_map(definition)
    normalized: Dict[str, Any] = {}
    unknown_values: List[Any] = []

    for raw_key, value in args.items():
        key = str(raw_key).strip()
        canonical = schema_map.get(key) or _ALIAS_TO_CANONICAL.get(key)
        if canonical and canonical not in normalized:
            normalized[canonical] = _coerce_scalar(value)
        else:
            unknown_values.append(value)

    if unknown_values:
        for param, value in zip(definition.parameters, unknown_values):
            if param.name not in normalized:
                normalized[param.name] = _coerce_scalar(value)

    return normalized


def validate_arguments(
    args: Dict[str, Any],
    definition: "ToolDefinition",
) -> Optional[str]:
    """按 schema 校验入参：required / type / enum。返回错误文案或 None。

    由 SecureToolExecutor 在 L2 统一调用 —— 此前这三类检查全靠各工具
    自己实现，写漏了就漏（2026-11-10 审计）。文案带纠正指引，
    模型收到后要么自行补参重试，要么向用户追问。
    """
    for param in definition.parameters:
        value = args.get(param.name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if param.required:
                label = param.description or param.name
                return (
                    f"缺少必填参数【{label}】。"
                    "请补全后重新调用，或向用户确认该信息。"
                )
            continue
        check = _TYPE_CHECKS.get(param.type)
        if check and not check(value):
            return (
                f"参数 {param.name} 类型应为 {param.type}，"
                f"收到 {type(value).__name__}。请修正后重新调用。"
            )
        if param.enum and value not in param.enum:
            options = "/".join(str(option) for option in param.enum)
            return f"参数 {param.name} 只能是：{options}。请修正后重新调用。"
    return None
