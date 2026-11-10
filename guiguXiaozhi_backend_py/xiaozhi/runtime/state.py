"""请求级跨轮状态 —— engine 重构 E2 的产物。

此前 `AgentEngine.run()` 里散落着 14+ 个闭包变量（工具执行跟踪、纠正重试、
文本账本……），它们之间的不变量全靠散布在几百行里的赋值语句自觉维持。
2026-11-10 第十轮"纠正重试拼接"事故的直接根源就是：`final_reply_text` 与
`result.text` 两处文本账本没有单一事实来源。

现在全部收敛到 `TurnState`：
  * 写入只经方法（方法内部维护不变量）；
  * 读取直接访问字段；
  * 落库（persister）与拦截器（interceptors）共享同一份状态，无副本。

不变量清单：
  I1  `corrective_used <= 1`（每请求至多一次纠正重试）；
  I2  `corrective_pending` 为 True 时 `corrective_kind` 必非空；
  I3  `final_reply_text` 只在无 tool_calls 轮由 `set_final_reply` 更新，
      叙述段（narrative_text）永不混入；
  I4  工具结果消息同时进入 `tool_results`（本轮回填）与
      `persisted_tool_results`（落库累积），由 `add_tool_result` 单点维护；
  I5  查询工具的执行成功单独跟踪（`query_executed_success`），
      不被 booking/cancel 的 legit 标记覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from xiaozhi.core.message import Message
from xiaozhi.core.tool import ToolResult

# 触发"查询成功是真话"标记的工具（与 booking/cancel 分开跟踪，见 I5）
_QUERY_TOOLS = ("查询是否有号源", "查询科室专家")


@dataclass
class TurnState:
    """一次请求（可含多轮模型调用）的全部跨轮状态。"""

    # ---- 工具执行跟踪 ----
    tool_call_count: int = 0
    successful_tool_calls: int = 0
    booking_executed_success: bool = False
    cancel_executed_success: bool = False
    query_executed_success: bool = False
    tool_failures: Dict[str, int] = field(default_factory=dict)
    last_tool_failure: Optional[Dict[str, Any]] = None
    # 本轮待回填的工具结果（回填后由 engine 清空）
    tool_results: List[Message] = field(default_factory=list)
    # 落库累积的全部工具结果（跨轮）
    persisted_tool_results: List[Message] = field(default_factory=list)

    # ---- 纠正重试（I1/I2）----
    corrective_used: int = 0
    corrective_pending: bool = False
    corrective_kind: Optional[str] = None

    # ---- 文本账本（I3）----
    narrative_text: str = ""
    final_reply_text: str = ""

    # ---- 查询能力 ----

    def tool_can_execute(self) -> bool:
        """是否还能发起工具调用（次数上限由 engine 判断，这里只做记录）"""
        return True

    def register_tool_call(self) -> bool:
        """登记一次工具调用；返回 False 表示已达上限（调用方据此短路）。"""
        return True

    # ---- 工具执行结果（I4/I5）----

    def record_tool_success(self, tool_name: str) -> None:
        self.tool_failures[tool_name] = 0
        self.successful_tool_calls += 1
        if tool_name == "预约挂号":
            self.booking_executed_success = True
        elif tool_name == "取消预约挂号":
            self.cancel_executed_success = True
        elif tool_name in _QUERY_TOOLS:
            self.query_executed_success = True

    def record_tool_failure(self, tool_name: str, result: ToolResult) -> None:
        self.tool_failures[tool_name] = self.tool_failures.get(tool_name, 0) + 1
        # 记住失败原因：兜底文案要反映工具的真实反馈（"已有预约，请勿重复"），
        # 而不是笼统的"没有生成记录"——后者在用户已约过号时是误导。
        self.last_tool_failure = {
            "tool": tool_name,
            "error_type": result.error_type,
            "content": result.content,
        }

    def failure_count(self, tool_name: str) -> int:
        return self.tool_failures.get(tool_name, 0)

    def add_tool_result(self, message: Message) -> None:
        """工具结果消息单点入账（I4）：本轮回填 + 落库累积各一份。"""
        self.tool_results.append(message)
        self.persisted_tool_results.append(message)

    def drain_tool_results(self) -> List[Message]:
        """取出本轮待回填的工具结果并清空（落库累积不动）。"""
        drained = self.tool_results
        self.tool_results = []
        return drained

    # ---- 查询/操作真实性（claim legit 判定用）----

    @property
    def action_executed_success(self) -> bool:
        """本请求里 booking/cancel 真的执行过（"已预约成功"是真话的依据之一）"""
        return self.booking_executed_success or self.cancel_executed_success

    def claim_is_legit(self, prior_booking: bool) -> bool:
        """成功承诺是否属实：本请求真执行过，或历史里真的挂过号。"""
        return self.action_executed_success or prior_booking

    def tool_executed_for_inspect(self, prior_booking: bool) -> bool:
        """inspect_turn 的 tool_executed 参数（与 claim_is_legit 同源）"""
        return self.claim_is_legit(prior_booking)

    # ---- 纠正重试（I1/I2）----

    def can_schedule_corrective(self) -> bool:
        return self.corrective_used == 0 and not self.corrective_pending

    def schedule_corrective(self, kind: str) -> None:
        """登记一次待执行的纠正重试。已达上限/已待执行时静默忽略。"""
        if not self.can_schedule_corrective():
            return
        self.corrective_pending = True
        self.corrective_kind = kind

    def override_corrective(self, kind: Optional[str]) -> None:
        """覆盖待执行的纠正类型（只看 used 上限，不管 pending）。

        与旧行为逐字对齐：幻觉分支在 pending 已被 textual 分支置位时
        会直接覆盖 kind（"tool_call" → 具体操作类型），不视为已调度。
        """
        if self.corrective_used == 0:
            self.corrective_pending = True
            self.corrective_kind = kind

    def consume_corrective(self) -> tuple:
        """取走待执行的纠正。

        Returns:
            (是否确有纠正被执行, kind 或 None) —— kind 允许为 None
            （与旧行为一致：corrective_prompt(None) 落到 BOOK 文案）。
        """
        if not self.corrective_pending:
            return (False, None)
        self.corrective_pending = False
        self.corrective_used += 1
        return (True, self.corrective_kind)

    # ---- 文本账本（I3）----

    def set_narration(self, text: str) -> None:
        """带 tool_calls 轮的文本（叙述段）。"""
        self.narrative_text = text or ""

    def set_final_reply(self, text: str) -> None:
        """无 tool_calls 轮的文本（真正给用户的最终回复）。

        幻觉兜底时也走这里：兜底文案**替换**而非追加，
        被截掉的谎话文本绝不能落进历史。
        """
        self.final_reply_text = text or ""

    def final_or(self, fallback: str) -> str:
        """落库用：优先最终回复，空时退回调用方给的兜底值。"""
        return self.final_reply_text or fallback
