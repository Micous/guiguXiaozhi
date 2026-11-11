"""参数校验 —— 三道关：Schema → 业务规则 → 语义合理。

关键设计：**校验失败不抛异常中断，而是返回错误信息回传给模型**，
让模型自己纠正后重试。这比框架默认的"抛异常打断"更稳，也更容易在 trace 里看到。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional

# 18 位身份证：前 17 位 + 校验位
ID_CARD_RE = re.compile(r"^[1-9]\d{5}(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}[\dXx]$")
ID_CARD_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
ID_CARD_CHECK_CHARS = "10X98765432"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_SLOTS = ("上午", "下午")


@dataclass
class ValidationResult:
    """校验结果"""

    ok: bool
    errors: List[str] = field(default_factory=list)
    normalized: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, **normalized: Any) -> "ValidationResult":
        return cls(ok=True, normalized=normalized)

    @classmethod
    def failure(cls, *errors: str) -> "ValidationResult":
        return cls(ok=False, errors=list(errors))

    def message(self) -> str:
        """给模型看的错误说明（用自然语言，便于模型自我纠正）"""
        return "；".join(self.errors)


def validate_id_card(value: str) -> ValidationResult:
    """身份证号校验（含校验位）"""
    value = (value or "").strip()
    if not value:
        return ValidationResult.failure("身份证号不能为空")
    if not ID_CARD_RE.match(value):
        return ValidationResult.failure(f"身份证号格式不正确：{value}，应为 18 位（最后一位可能是 X）")
    body, check = value[:17], value[17].upper()
    total = sum(int(c) * w for c, w in zip(body, ID_CARD_WEIGHTS))
    if ID_CARD_CHECK_CHARS[total % 11] != check:
        return ValidationResult.failure(f"身份证号校验位不正确：{value}")
    return ValidationResult.success(id_card=value)


def validate_date(value: str, allow_past: bool = False) -> ValidationResult:
    """日期校验：格式 + 不能早于今天"""
    value = (value or "").strip()
    if not value:
        return ValidationResult.failure("预约日期不能为空")
    if not DATE_RE.match(value):
        return ValidationResult.failure(f"日期格式不正确：{value}，应为 2026-11-20 这样的 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return ValidationResult.failure(f"日期不存在：{value}")
    if not allow_past and parsed < date.today():
        return ValidationResult.failure(f"预约日期 {value} 已经过去了，请选择今天或之后的日期")
    return ValidationResult.success(date=value)


def validate_time_slot(value: str) -> ValidationResult:
    """时段校验"""
    value = (value or "").strip()
    if value in TIME_SLOTS:
        return ValidationResult.success(time=value)
    if value in ("早上", "早"):
        return ValidationResult.success(time="上午")
    if value in ("中午",):
        return ValidationResult.success(time="上午")
    if not value:
        return ValidationResult.failure("预约时段不能为空，只能是“上午”或“下午”")
    return ValidationResult.failure(f"预约时段只能是“上午”或“下午”，收到的是“{value}”")


def validate_username(value: str) -> ValidationResult:
    """姓名校验"""
    value = (value or "").strip()
    if not value:
        return ValidationResult.failure("患者姓名不能为空")
    if len(value) > 32:
        return ValidationResult.failure("患者姓名过长")
    if any(ch.isdigit() for ch in value):
        return ValidationResult.failure("患者姓名不应包含数字")
    return ValidationResult.success(username=value)


# 科室名的常见**门诊类型**修饰语，去掉后再比对。
# 注意：不能剥掉"科" —— "儿科"剥掉就变成"儿"，会跟任何以"儿"结尾的名字撞上。
_DEPARTMENT_SUFFIXES = ("专家门诊", "普通门诊", "门诊")


def _normalize_department(value: str) -> str:
    """把科室名归一化：去掉"专家门诊/普通门诊/门诊"等门诊类型修饰语"""
    cleaned = (value or "").strip()
    for suffix in _DEPARTMENT_SUFFIXES:
        cleaned = cleaned.replace(suffix, "")
    return cleaned.strip()


def validate_department(value: str, known: Optional[List[str]] = None) -> ValidationResult:
    """科室校验（known 为空时只做非空校验）

    **必须与实际能挂号的名录精确比对**（2026-11-10 真实联调踩到）：
    旧实现用双向子串匹配 `"内科" in "神经内科"` 判定通过，
    于是"内科"这种**本院并不存在的科室**放过了校验，
    工具白跑一次才在号源服务那里失败（用户白等一轮）。
    现在改成"归一化后精确相等"，让校验层与号源服务的判定**保持一致**。
    """
    value = (value or "").strip()
    if not value:
        return ValidationResult.failure("预约科室不能为空")
    if known:
        cleaned = _normalize_department(value)
        candidates = {_normalize_department(k) for k in known}
        if cleaned not in candidates:
            return ValidationResult.failure(
                f"本院没有【{value}】这个科室，可选科室有：{'、'.join(sorted(known))}"
            )
    return ValidationResult.success(department=value)


def validate_doctor(value: Optional[str]) -> ValidationResult:
    """医生姓名（可选）"""
    if value is None or not str(value).strip():
        return ValidationResult.success(doctor_name=None)
    value = str(value).strip()
    if len(value) > 32:
        return ValidationResult.failure("医生姓名过长")
    return ValidationResult.success(doctor_name=value)


def validate_appointment(
    params: Dict[str, Any],
    known_departments: Optional[List[str]] = None,
) -> ValidationResult:
    """预约参数整体校验（三道关汇总）"""
    errors: List[str] = []
    normalized: Dict[str, Any] = {}

    checks = [
        (validate_username(params.get("username", "")), "username"),
        (validate_id_card(params.get("id_card", "")), "id_card"),
        (validate_department(params.get("department", ""), known_departments), "department"),
        (validate_date(params.get("date", "")), "date"),
        (validate_time_slot(params.get("time", "")), "time"),
        (validate_doctor(params.get("doctor_name")), "doctor_name"),
    ]
    for result, key in checks:
        if not result.ok:
            errors.extend(result.errors)
        else:
            normalized[key] = result.normalized.get(key)

    if errors:
        return ValidationResult(ok=False, errors=errors)
    return ValidationResult(ok=True, normalized=normalized)
