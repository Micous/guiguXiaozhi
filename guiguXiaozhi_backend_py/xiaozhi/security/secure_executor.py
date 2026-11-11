"""安全执行器 —— 四层安全流（对齐参考项目 mini_harness/security/secure_executor.py）。

    L1 权限决策  →  L2 参数校验  →  L3 注入防护  →  L4 执行 + 审计

任何一层不通过，都不会真正执行工具；拒绝原因会作为结构化结果返回给模型，
让它自己调整，而不是让整轮对话失败。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from xiaozhi.core.tool import Tool, ToolResult
from xiaozhi.ops.audit import AuditLogger
from xiaozhi.security.approval_service import (
    ApprovalRejected,
    ApprovalService,
    get_approval_context,
)
from xiaozhi.security.injection import detect_injection
from xiaozhi.security.principal import CHAT_WRITE_SCOPE
from xiaozhi.security.permissions import (
    Decision,
    PermissionDecisionEngine,
    approval_scope_for_args,
)
from xiaozhi.tools.argument_normalizer import (
    normalize_arguments_by_schema,
    validate_arguments,
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
        audit_logger: Optional[AuditLogger] = None,
        approval_service: Optional[ApprovalService] = None,
    ) -> None:
        # no_ui_policy 是安全兜底开关，拼错字符串等于静默改策略 —— 启动即报错
        if no_ui_policy not in ("allow", "deny"):
            raise ValueError(
                f"no_ui_policy 只能是 'allow' 或 'deny'，收到：{no_ui_policy!r}"
            )
        self.permission_engine = permission_engine
        self.no_ui_policy = no_ui_policy
        self.approval_callback = approval_callback
        self.audit_logger = audit_logger
        # Phase 2 灰度：未装配审批服务时，下面整段门控完全不进入执行路径
        self.approval_service = approval_service

    def _audit(self, event_type: str, **fields: Any) -> None:
        if self.audit_logger is not None:
            self.audit_logger.record(event_type, **fields)

    async def execute(self, tool_call: ToolCall, tool: Tool) -> ToolResult:
        """执行一次工具调用（四层安全流 + Phase 2 审批门控）"""
        scope = approval_scope_for_args(tool_call.args)
        definition = tool.definition()

        # ---- L0 审批门控（灰度：approval_service=None 时整段不进入执行路径）----
        # 写工具必须先有「用户明确确认」签发的、绑定本次参数的一次性 token。
        # 模型自述「已同意」、请求体里塞 approved=true、YAML 里的 OVERRIDE
        # 都不能绕过这里 —— 决策只认 ApprovalService 的持久化状态。
        approval_granted = False
        if self.approval_service is not None and definition.is_write:
            # 审批绑定的是「实际执行的参数」，所以归一化必须前置：
            # 否则模型传 camelCase 别名时会出现「确认 A、执行 B」。
            canonical_args = normalize_arguments_by_schema(
                dict(tool_call.args or {}), definition
            )
            gate, effective_args = self._approval_gate(tool_call, canonical_args)
            if gate is not None:
                return gate
            if effective_args is not None:
                # 审批确认的参数优先（模型两轮输出不一致时，以用户确认为准）
                canonical_args = effective_args
            tool_call.args = canonical_args
            approval_granted = True

        # ---- L1 权限决策 ----
        decision = self.permission_engine.decide(
            tool_call.tool_name, tool_call.user_id, scope, tool_call.trace_id
        )
        self._audit(
            "permission_decision",
            decision=decision.value,
            tool_name=tool_call.tool_name,
            user_id=tool_call.user_id,
            trace_id=tool_call.trace_id,
            args=tool_call.args,
        )
        if decision == Decision.DENY:
            return ToolResult.fail(
                f"工具【{tool_call.tool_name}】已被安全策略禁止调用。",
                error_type="PermissionDenied",
            )
        if decision == Decision.ASK_USER:
            if approval_granted:
                # 已通过持久化审批闭环核验，不再走「无 UI 放行」这条兜底路径
                logger.info("审批闭环已核验，跳过 ASK：tool=%s", tool_call.tool_name)
            elif self.approval_callback is None:
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
        # 先按 schema 归一化（参数名/camel/中文描述别名/位置兜底），再校验
        # required/type/enum，最后做通用体检。工具内部的旧版归一化保持
        # 幂等兼容 —— 双层归一化结果一致。
        tool_call.args = normalize_arguments_by_schema(tool_call.args, definition)
        invalid = validate_arguments(tool_call.args, definition)
        if invalid:
            return ToolResult.fail(invalid, error_type="ValidationError")
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

        # ---- L4 执行 + 审计 ----
        try:
            result = await tool.timed_call(tool_call.args)
        except Exception as exc:  # 兜底：工具异常不外泄
            logger.exception("工具执行异常")
            result = ToolResult.fail(
                f"工具执行异常：{type(exc).__name__}: {exc}", error_type="ToolExecutionError"
            )
        self._audit(
            "tool_execution",
            tool_name=tool_call.tool_name,
            user_id=tool_call.user_id,
            trace_id=tool_call.trace_id,
            success=result.success,
            error_type=result.error_type,
            duration_ms=round(result.execution_time * 1000, 2),
            result_preview=result.content[:200],
        )
        return result

    def _approval_gate(
        self, tool_call: ToolCall, canonical_args: Dict[str, Any]
    ) -> Tuple[Optional[ToolResult], Optional[Dict[str, Any]]]:
        """L0：写工具执行前的授权闭环核验。

        返回 ``(拒绝结果, 生效参数)``：
          * 拒绝结果非 None → 不允许执行；
          * 生效参数非 None → **覆盖**本次执行的参数（用户确认过的那组）。

        决策只认 ``ApprovalService`` 的持久化状态 + contextvar 里的请求级上下文：
          * 模型自述「用户已经同意」、请求体塞 ``approved=true``、
            YAML 里的 OVERRIDE 策略、进程内的 ``_approvals`` 缓存，
            一律不能替代这里（审计 P0-02 验收 1/7）。
          * 无 token → 生成 PENDING 请求并拒绝执行，由前端确认 UI 展示；
          * 有 token → 原子消费；参数与确认时不一致时，**以审批记录里的
            参数执行**（用户确认的是哪组参数就按哪组执行，2026-11-11 真机事故）。
        """
        ctx = get_approval_context()
        principal = ctx.principal
        session_id = ctx.session_id or tool_call.user_id or "default"

        # 验收 6：缺 required scope 直接拒绝，且发生在任何执行尝试之前
        if ctx.auth_enabled and not principal.has_scope(CHAT_WRITE_SCOPE):
            return (
                self._approval_denied(
                    "missing_scope", "当前账号缺少 chat:write 权限，写操作未执行。"
                ),
                None,
            )

        if ctx.approval_token:
            try:
                _record, effective_args = self.approval_service.consume(
                    ctx.approval_token,
                    principal,
                    session_id=session_id,
                    tool_name=tool_call.tool_name,
                    canonical_args=canonical_args,
                )
            except ApprovalRejected as exc:
                logger.warning(
                    "审批消费失败：tool=%s reason=%s trace=%s",
                    tool_call.tool_name,
                    exc.reason_code,
                    tool_call.trace_id,
                )
                return (
                    self._approval_denied(
                        exc.reason_code,
                        f"审批校验未通过（{exc.reason_code}），操作未执行。",
                    ),
                    None,
                )
            # 以**用户确认过的那组参数**执行（模型两轮输出不一致时尤其重要）
            if effective_args != canonical_args:
                logger.info(
                    "按审批确认的参数执行：tool=%s", tool_call.tool_name
                )
            logger.info("审批一次性消费成功：tool=%s", tool_call.tool_name)
            return None, effective_args

        record = self.approval_service.create(
            principal,
            session_id,
            tool_call.tool_name,
            canonical_args,
            required_scopes=[CHAT_WRITE_SCOPE],
        )
        detail = self.approval_service.detail(record.approval_id, principal)
        self._audit(
            "approval_required",
            tool_name=tool_call.tool_name,
            user_id=tool_call.user_id,
            trace_id=tool_call.trace_id,
            approval_id=record.approval_id,
        )
        return (
            ToolResult.fail(
                f"工具【{tool_call.tool_name}】需要您核对信息并明确确认，尚未执行。",
                error_type="ApprovalRequired",
                approval={
                    "decision": "REQUIRE_APPROVAL",
                    "reason_code": "pending_confirmation",
                    "detail": detail,
                    "message": (
                        f"该操作（{tool_call.tool_name}）需要您核对信息并明确确认后才会执行，"
                        "请在确认窗口核对参数；未确认前不会挂号或取消。"
                    ),
                },
            ),
            None,
        )

    @staticmethod
    def _approval_denied(reason_code: str, message: str) -> ToolResult:
        return ToolResult.fail(
            message,
            error_type="ApprovalRejected",
            approval={"decision": "DENY", "reason_code": reason_code, "message": message},
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
