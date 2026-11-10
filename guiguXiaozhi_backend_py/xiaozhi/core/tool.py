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
    """工具定义 —— 直接可序列化为 Ollama / OpenAI 的 tools 字段"""

    name: str
    description: str
    parameters: List[ToolParameter] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.READ_ONLY
    permissions_required: List[str] = field(default_factory=list)
    timeout_seconds: int = 30

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
