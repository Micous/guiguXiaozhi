"""工具注册中心 —— 对齐参考项目 mini_harness/tools/registry.py。

除了注册与查找，还负责：
  * 缓存工具的 JSON Schema（模型每次请求都要带，省得重复构造）
  * 给出**给模型看的**工具清单
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from xiaozhi.core.tool import Tool, ToolDefinition


class ToolRegistry:
    """工具注册中心"""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        self._definitions: Dict[str, ToolDefinition] = {}

    def register(self, tool: Tool) -> None:
        """注册工具"""
        definition = tool.definition()
        self._tools[definition.name] = tool
        self._definitions[definition.name] = definition

    def register_all(self, tools: List[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def definition(self, name: str) -> Optional[ToolDefinition]:
        return self._definitions.get(name)

    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())

    def list_definitions(self) -> List[ToolDefinition]:
        return list(self._definitions.values())

    def tool_payloads(self) -> List[Dict[str, Any]]:
        """转成 Ollama /api/chat 的 tools 数组"""
        return [d.to_tool_payload() for d in self._definitions.values()]

    def validate_contracts(self) -> List[str]:
        """Phase 2：注册期契约体检，返回全部问题（组合根在审批开启时 fail-fast）。

        不做静默降级：未知风险级别 / 未声明幂等的写工具，宁可拒绝启动也不能
        让执行器在运行时猜「这个工具到底能不能写」。
        """
        problems: List[str] = []
        for name, definition in self._definitions.items():
            reason = definition.validate_contract()
            if reason:
                problems.append(f"{name}: {reason}")
        return problems

    def __len__(self) -> int:
        return len(self._tools)
