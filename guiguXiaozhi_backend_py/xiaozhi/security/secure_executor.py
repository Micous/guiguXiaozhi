"""安全执行器 —— 四层安全流（对齐参考项目 mini_harness/security/secure_executor.py）。

    L1 权限决策  →  L2 参数校验  →  L3 注入防护  →  L4 执行 + 审计

任何一层不通过，都不会真正执行工具；拒绝原因会作为结构化结果返回给模型，
让它自己调整，而不是让整轮对话失败。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from xiaozhi.core.tool import Tool, ToolResult
from xiaozhi.security.injection import detect_injection
from xiaozhi.security.permissions import (
    Decision,
    PermissionDecisionEngine,
    approval_scope_for_args,
)

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """一次工具调用"""

    tool_name: str
    args: Dict[str, Any]
    user_id: str = "anonymous"
    trace_id: str = ""


class SecureToolExecutor:
    """把权限、校验、注入防护串起来的工具执行器"""

    def __init__(
        self,
        permission_engine: PermissionDecisionEngine,
        no_ui_policy: str = "allow",
        approval_callback: Optional[Callable[[str, ToolCall], Awaitable[bool]]] = None,
    ) -> None:
        self.permission_engine = permission_engine
        self.no_ui_policy = no_ui_policy
        self.approval_callback = approval_callback

    async def execute(self, tool_call: ToolCall, tool: Tool) -> ToolResult:
        """执行一次工具调用（四层安全流）"""
        scope = approval_scope_for_args(tool_call.args)

        # ---- L1 权限决策 ----
        decision = self.permission_engine.decide(
            tool_call.tool_name, tool_call.user_id, scope, tool_call.trace_id
        )
        if decision == Decision.DENY:
            return ToolResult.fail(
                f"工具【{tool_call.tool_name}】已被安全策略禁止调用。",
                error_type="PermissionDenied",
            )
        if decision == Decision.ASK_USER:
            if self.approval_callback is None:
                # 无交互通道：按配置的兜底策略处理
                if self.no_ui_policy == "allow":
                    logger.info(
                        "无交互通道，按 no_ui=allow 放行工具 %s", tool_call.tool_name
                    )
                else:
                    return ToolResult.fail(
                        f"工具【{tool_call.tool_name}】需要用户确认，但当前没有确认通道。",
                        error_type="ApprovalRequired",
                    )
            else:
                approved = await self.approval_callback(tool_call.user_id, tool_call)
                if not approved:
                    return ToolResult.fail(
                        f"用户拒绝了工具【{tool_call.tool_name}】的调用。",
                        error_type="UserRejected",
                    )
                self.permission_engine.record_approval(
                    tool_call.user_id, tool_call.tool_name, scope, tool_call.trace_id
                )

        # ---- L2 参数校验（注入防护在 L3）----
        # 校验放在工具内部完成（工具知道自己要什么），这里只做**通用**入参体检
        invalid = self._validate_generic(tool_call)
        if invalid:
            return ToolResult.fail(invalid, error_type="ValidationError")

        # ---- L3 注入防护 ----
        for key, value in tool_call.args.items():
            if not isinstance(value, str):
                continue
            finding = detect_injection(value)
            if finding.detected:
                logger.warning(
                    "检测到疑似注入：trace=%s tool=%s key=%s reason=%s",
                    tool_call.trace_id,
                    tool_call.tool_name,
                    key,
                    finding.reason,
                )
                return ToolResult.fail(
                    "输入中包含疑似越权指令，已拒绝执行。",
                    error_type="PromptInjection",
                )

        # ---- L4 执行 ----
        try:
            return await tool.timed_call(tool_call.args)
        except Exception as exc:  # 兜底：工具异常不外泄
            logger.exception("工具执行异常")
            return ToolResult.fail(
                f"工具执行异常：{type(exc).__name__}: {exc}", error_type="ToolExecutionError"
            )

    @staticmethod
    def _validate_generic(tool_call: ToolCall) -> Optional[str]:
        """通用入参体检：参数必须是字符串、长度受限、键名受限"""
        for key, value in tool_call.args.items():
            if value is None:
                continue
            if not isinstance(value, str):
                return f"参数 {key} 必须是字符串，收到的是 {type(value).__name__}。"
            if len(value) > 512:
                return f"参数 {key} 过长（超过 512 字符）。"
        return None
