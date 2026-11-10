"""SecureToolExecutor 四层安全流单测（核心模块第 2 批）。

重点验证 L2 新接线：schema 归一化（中文别名/位置兜底）在工具执行前统一完成，
schema 校验在工具内部自校验之前兜底。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from xiaozhi.core.tool import RiskLevel, Tool, ToolDefinition, ToolParameter, ToolResult  # noqa: E402
from xiaozhi.security.permissions import PermissionDecisionEngine, PermissionLevel  # noqa: E402
from xiaozhi.security.secure_executor import SecureToolExecutor, ToolCall  # noqa: E402


class RecordingTool(Tool):
    """记录收到的参数，返回原样内容"""

    def __init__(self) -> None:
        self.received: dict | None = None
        self.call_count = 0

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="测试工具",
            description="测试",
            parameters=[
                ToolParameter("username", "string", "患者姓名"),
                ToolParameter("date", "string", "预约日期"),
            ],
            risk_level=RiskLevel.READ_ONLY,
        )

    async def call(self, params):
        self.call_count += 1
        self.received = dict(params)
        return ToolResult.ok(f"收到 {params.get('username')}")


def _executor(tool_level: PermissionLevel = PermissionLevel.AUTO) -> tuple:
    engine = PermissionDecisionEngine(policies={"测试工具": tool_level})
    executor = SecureToolExecutor(engine, no_ui_policy="deny")
    return executor, RecordingTool()


async def test_executor_normalizes_chinese_alias_keys_before_tool_call():
    """中文描述名参数在执行前被归一化为规范名 —— 工具收到的键是干净的"""
    executor, tool = _executor()
    result = await executor.execute(
        ToolCall(tool_name="测试工具", args={"患者姓名": "王新勃", "日期": "2026-11-20"}),
        tool,
    )
    assert result.success
    assert tool.received == {"username": "王新勃", "date": "2026-11-20"}


async def test_executor_rejects_missing_required_before_tool_runs():
    """required 缺失 → L2 直接拒绝，工具一次都不执行"""
    executor, tool = _executor()
    result = await executor.execute(
        ToolCall(tool_name="测试工具", args={"username": "王新勃"}),
        tool,
    )
    assert not result.success
    assert result.error_type == "ValidationError"
    assert tool.call_count == 0  # 没到工具层


async def test_executor_rejects_non_string_values():
    executor, tool = _executor()
    result = await executor.execute(
        ToolCall(tool_name="测试工具", args={"username": ["王新勃"], "date": "2026-11-20"}),
        tool,
    )
    assert not result.success
    assert result.error_type == "ValidationError"
    assert tool.call_count == 0


async def test_executor_denied_tool_never_runs():
    """策略 DENY → L1 拒绝，L2/L3/L4 都不进"""
    executor, tool = _executor(PermissionLevel.DENY)
    result = await executor.execute(
        ToolCall(tool_name="测试工具", args={"username": "王新勃", "date": "2026-11-20"}),
        tool,
    )
    assert not result.success
    assert result.error_type == "PermissionDenied"
    assert tool.call_count == 0
