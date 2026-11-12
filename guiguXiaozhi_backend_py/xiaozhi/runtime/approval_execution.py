"""审批确认后的确定性执行 —— 「用户确认了，就一定要真的执行」。

真机事故（2026-11-12，8081 + 5173 手工操作）：

    轮1 用户点「确认执行」→ 前端带一次性 token 重发
    轮1 模型：请在确认窗口核对上述信息并确认预约。未确认前不会进行实际挂号操作。
    轮1 引擎：追加强制执行指令（``_retry_with_approval_hint``）
    轮2 模型：已收到您的确认，正在为您提交预约，请稍候。
    → 两轮都零工具调用：token 白拿、预约没有发生，用户却被告知"正在提交"。

``_retry_with_approval_hint`` 是"求模型配合"，模型不配合就必然失败 ——
而这件事本来就**不需要模型配合**：

    * token 是用户亲手点下「确认」后由 ``ApprovalService`` 签发的凭据；
    * 参数是用户在确认窗里核对过的那一组，原样落在审批记录
      （``ApprovalRecord.canonical_args``）里；
    * 执行器要做的事只有一件：**按这条记录执行**。

于是兜底方案是「服务端拿着审批记录直接执行」，模型这一轮说什么都不再重要。

安全边界一个字都没放宽 —— 执行仍走 ``SecureToolExecutor`` 的四层安全流：

    L0  原子消费一次性 token（重放/过期/越权/跨会话/跨工具照样拒绝）
    L1  权限决策（缺 chat:write 照样拒绝）
    L2  参数校验        L3 注入防护        L4 执行 + 审计

变的只是"谁发起这次调用"：从"模型愿不愿意开口"变成"用户已经确认过"。

本模块只提供**回复文案**（纯函数，便于单测）：执行、事件、落库仍在
``EnginePhases._execute_approved_operation`` 里编排。
"""

from __future__ import annotations

import re

from xiaozhi.core.tool import ToolResult
from xiaozhi.runtime.response_gate import UNKNOWN_NOTICE
from xiaozhi.security.masking import mask_pii_in_text

#: 执行成功但没有可用文案时的兜底
APPROVED_DONE_DEFAULT = "已按您的确认执行完成。"

#: 执行未成功时的模板（必须如实说"没办成"，绝不允许包装成成功）
APPROVED_FAILED_TEMPLATE = "已按您的确认执行，但未能完成：{reason}"

#: 工具结果是回给**模型**的（"请根据提示修正参数后重新调用"），
#: 直接念给用户会莫名其妙 —— 收尾时剥掉这类尾巴。
_MODEL_DIRECTED_TAIL_RE = re.compile(
    r"[，,；;]?\s*请(?:根据提示[^。！？]*|重新(?:调用|尝试|提交)[^。！？]*)[。！？]?$"
)


def strip_model_directed(text: str) -> str:
    """剥掉面向模型的指令尾句（"请根据提示修正参数后重新调用。"）。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    return _MODEL_DIRECTED_TAIL_RE.sub("", cleaned).strip()


def approved_operation_reply(result: ToolResult) -> str:
    """把工具结果转成**面向用户**的最终回复（确定性，不经模型）。

    三条口径：
      * UNKNOWN（HIS 对账语义）→ 固定文案"结果核实中，请勿重复提交"，
        既不说成功也不说失败（审计 P0-05 验收 3/6）；
      * 成功（含"已有预约"的幂等命中，工具返回的 content 本身就是
        给用户看的详情，身份证号已打码）→ 原文下发；
      * 失败 → 如实说"没能完成"+ 原因，去掉面向模型的收尾指令。

    最后统一过一遍 PII 打码：这是"工具结果 → 用户可见文本"的最后一道口子。
    """
    metadata = result.metadata or {}
    if metadata.get("authoritative_status") == "UNKNOWN":
        return UNKNOWN_NOTICE

    content = strip_model_directed(result.content)
    if result.success:
        reply = content or APPROVED_DONE_DEFAULT
    else:
        reply = (
            APPROVED_FAILED_TEMPLATE.format(reason=content)
            if content
            else "已按您的确认执行，但未能完成，请稍后再试或到人工窗口办理。"
        )
    return mask_pii_in_text(reply)
