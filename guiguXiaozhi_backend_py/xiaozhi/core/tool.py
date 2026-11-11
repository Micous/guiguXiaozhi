"""工具抽象：Tool ABC / ToolDefinition / ToolResult。

对齐参考项目 mini_harness/core/tool.py，并补上硅谷小智上线需要的字段：
风险等级、所需权限、入参 JSON Schema 的业务校验器挂载点。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional


class RiskLevel(str, Enum):
    """工具风险等级 —— 权限决策的输入"""

    READ_ONLY = "read_only"     # 只读查询，无副作用
    WRITE = "write"             # 写业务库（预约/取消）
    DESTRUCTIVE = "destructive" # 删除类，需最高等级控制


@dataclass
class ToolResult:
    """工具执行结果"""

    success: bool
    content: str
    execution_time: float = 0.0
    error_type: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return not self.success

    @classmethod
    def ok(cls, content: str, **metadata: Any) -> "ToolResult":
        return cls(success=True, content=content, metadata=metadata)

    @classmethod
    def fail(
        cls,
        content: str,
        error_type: str = "ToolError",
        **metadata: Any,
    ) -> "ToolResult":
        return cls(success=False, content=content, error_type=error_type, metadata=metadata)


@dataclass
class ToolParameter:
    """单个入参定义"""

    name: str
    type: str = "string"
    description: str = ""
    required: bool = True
    enum: Optional[List[Any]] = None

    def to_schema(self) -> Dict[str, Any]:
        schema: Dict[str, Any] = {"type": self.type}
        if self.description:
            schema["description"] = self.description
        if self.enum:
            schema["enum"] = self.enum
        return schema


@dataclass
class ToolDefinition:
    """工具定义 —— 直接可序列化为 Ollama / OpenAI 的 tools 字段。

    Phase 2（审计 4.2）：补上副作用/审批/幂等等契约元数据。原有字段语义与默认值
    保持不变，新字段全部带默认值 —— 灰度关闭时既有工具零改动、行为零变更。
    """

    name: str
    description: str
    parameters: List[ToolParameter] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.READ_ONLY
    permissions_required: List[str] = field(default_factory=list)
    timeout_seconds: int = 30
    # ---- 副作用与审批契约 ----
    # effect 留空时由 risk_level 推导；显式写错会在注册时立刻报错，不静默放行
    effect: str = ""
    approval_required: bool = False
    idempotency_required: bool = False
    result_schema: Dict[str, Any] = field(default_factory=dict)
    retry_policy: str = "never"  # never | safe_idempotent | query_before_retry

    def __post_init__(self) -> None:
        if not self.effect:
            self.effect = (
                "read_only" if self.risk_level == RiskLevel.READ_ONLY else "write"
            )
        if self.effect not in ("read_only", "write"):
            raise ValueError(f"工具 {self.name} 的 effect 非法：{self.effect!r}")
        if not isinstance(self.risk_level, RiskLevel):
            raise ValueError(f"工具 {self.name} 的 risk_level 未知：{self.risk_level!r}")
        if self.retry_policy not in ("never", "safe_idempotent", "query_before_retry"):
            raise ValueError(f"工具 {self.name} 的 retry_policy 非法：{self.retry_policy!r}")

    @property
    def is_write(self) -> bool:
        """是否会产生业务副作用（写库/删除）—— 审批门控的唯一判据"""
        return self.effect == "write"

    @property
    def required_scopes(self) -> List[str]:
        """审计契约名；沿用既有 permissions_required，避免两套字段漂移"""
        return self.permissions_required

    def validate_contract(self) -> Optional[str]:
        """注册期契约体检：未声明/未知风险与副作用 → 返回拒绝原因。

        只做「能不能安全判定」的检查，不强制写工具一律需要审批 —— 审批是否需要
        由审批开关 + ``approval_required`` 显式决定，便于灰度与只读工具平滑接入。
        """
        if not self.name or not self.description:
            return "工具名或描述为空"
        if not isinstance(self.risk_level, RiskLevel):
            return "未知风险级别"
        if self.effect not in ("read_only", "write"):
            return "未知副作用类型"
        if self.is_write and not self.idempotency_required:
            return "写工具未声明幂等策略"
        return None

    @property
    def input_schema(self) -> Dict[str, Any]:
        """生成 JSON Schema（object 类型）"""
        return {
            "type": "object",
            "properties": {p.name: p.to_schema() for p in self.parameters},
            "required": [p.name for p in self.parameters if p.required],
        }

    def to_tool_payload(self) -> Dict[str, Any]:
        """转成 Ollama /api/chat 的 tools 数组元素"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


class Tool(ABC):
    """工具抽象基类"""

    @abstractmethod
    async def call(self, params: Dict[str, Any]) -> ToolResult:
        """执行工具"""

    @abstractmethod
    def definition(self) -> ToolDefinition:
        """返回工具定义"""

    # ---------- 便捷实现 ----------

    def name(self) -> str:
        return self.definition().name

    def description(self) -> str:
        return self.definition().description

    def input_schema(self) -> Dict[str, Any]:
        return self.definition().input_schema

    async def timed_call(self, params: Dict[str, Any]) -> ToolResult:
        """带耗时统计的执行入口"""
        start = time.perf_counter()
        try:
            result = await self.call(params)
        except Exception as exc:  # 兜底：工具异常不外泄，统一转成错误结果
            result = ToolResult.fail(
                f"工具执行异常：{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
            )
        result.execution_time = time.perf_counter() - start
        return result
