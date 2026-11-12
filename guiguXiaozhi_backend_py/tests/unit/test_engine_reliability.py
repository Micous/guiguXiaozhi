"""智能体循环的可靠性行为单测 —— 全部用 fake，不依赖 Ollama / Milvus / 数据库。

覆盖：空响应重试、已下发文本时不重试、工具级连续失败熔断、模型熔断降级、
幻觉承诺提示、指标埋点。
"""

from __future__ import annotations

import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.core.event import EventType  # noqa: E402
from xiaozhi.core.message import Message, Role, ToolCall  # noqa: E402
from xiaozhi.core.tool import ToolResult  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402
from xiaozhi.models.quality import (  # noqa: E402
    FALLBACK_BOOKING_NOT_DONE,
    FALSE_CONFIRMATION_NOTICE,
)
from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.reliability.resilience import CircuitBreaker, RetryConfig  # noqa: E402
from xiaozhi.runtime.engine import AgentEngine  # noqa: E402


class FakeChatModel:
    """脚本化模型：每个元素 {"chunks": [...], "turn": ChatTurn, "error": Exception}"""

    model = "fake-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def stream_chat(self, messages, tools=None, result_holder=None):
        self.calls += 1
        spec = self.script.pop(0) if self.script else {"turn": ChatTurn(text="兜底回答")}
        for chunk in spec.get("chunks", []):
            yield chunk
        if spec.get("error"):
            raise spec["error"]
        if result_holder is not None:
            result_holder["turn"] = spec.get("turn", ChatTurn())


class FakeAssembled:
    history = []
    dropped_items = 0
    total_tokens = 0

    def to_messages(self, user_input, *, neutralize_history: bool = False):
        self.neutralize_history = neutralize_history
        return [{"role": "user", "content": user_input}]


class FakeAssembler:
    def assemble(self, *args, **kwargs):
        return FakeAssembled()


class FakeStore:
    def __init__(self, history=None):
        self.history = history or []

    async def load(self, memory_id, limit=20):
        return list(self.history)

    async def save(self, memory_id, messages):
        self.saved = messages

    async def close(self):
        return None


class FakeRegistry:
    def list_definitions(self):
        return []

    def get(self, name):
        return object()


class FakeExecutor:
    def __init__(self, result=None):
        self.result = result
        self.calls = 0

    async def execute(self, call, tool):
        self.calls += 1
        return self.result if self.result is not None else ToolResult.ok("OK")


def make_engine(model, **kwargs):
    metrics = kwargs.pop("metrics", None) or MetricsCollector()
    engine = AgentEngine(
        chat_model=model,
        tool_registry=FakeRegistry(),
        secure_executor=kwargs.pop("executor", None) or FakeExecutor(),
        conversation_store=kwargs.pop("store", None) or FakeStore(),
        context_assembler=FakeAssembler(),
        system_prompt="系统提示",
        retriever=kwargs.pop("retriever", None),
        metrics=metrics,
        retry_config=kwargs.pop("retry_config", None) or RetryConfig(max_attempts=1),
        circuit_breaker=kwargs.pop("breaker", None),
        tool_failure_threshold=kwargs.pop("tool_failure_threshold", 3),
        # 预约科室守卫默认关闭；新护栏单测需要显式注入名单提供者
        department_provider=kwargs.pop("department_provider", None),
    )
    return engine, metrics


# 默认输入**不能用纯问候语**：引擎对"你好"走确定性快路径（不进模型、不重试），
# 这些可靠性用例就测不到空响应重试 / 熔断降级 / 指标埋点了。
# 「查询号源」同属操作类（不注入 RAG），保持与旧默认值一致的上下文判定。
async def collect(engine, message="查询号源"):
    parts = []
    events = []
    async for event in engine.run("memory-1", message):
        events.append(event)
        if event.event_type == EventType.TEXT_RESPONSE:
            parts.append(event.metadata.get("delta", ""))
    return "".join(parts), events


async def test_empty_response_is_retried_without_duplicate_text():
    answer = "你好，请问有什么可以帮您？"
    model = FakeChatModel(
        [
            {"turn": ChatTurn()},  # Ollama 已知行为：200 但内容为空
            {"chunks": [answer], "turn": ChatTurn(text=answer)},
        ]
    )
    engine, metrics = make_engine(
        model, retry_config=RetryConfig(max_attempts=3, base_delay=0, jitter=0)
    )
    text, _ = await collect(engine)

    assert text == answer  # 没有重复文本
    assert model.calls == 2
    assert metrics.snapshot()["counters"]["empty_response_retries"] == 1


async def test_no_retry_once_text_has_been_streamed():
    model = FakeChatModel(
        [
            {"chunks": ["部分内容"], "error": RuntimeError("流中断")},
            {"chunks": ["重试内容"], "turn": ChatTurn(text="重试内容")},
        ]
    )
    engine, metrics = make_engine(
        model, retry_config=RetryConfig(max_attempts=3, base_delay=0, jitter=0)
    )
    text, _ = await collect(engine)

    assert model.calls == 1
    assert "部分内容" in text
    assert "重试内容" not in text
    assert metrics.snapshot()["counters"]["degraded_total"] == 1


async def test_tool_is_circuit_broken_after_consecutive_failures():
    calls = [
        ToolCall(id=str(i), name="查询是否有号源", arguments={}) for i in range(3)
    ]
    model = FakeChatModel(
        [
            {"turn": ChatTurn(tool_calls=calls)},
            {"chunks": ["已处理"], "turn": ChatTurn(text="已处理")},
        ]
    )
    executor = FakeExecutor(ToolResult.fail("号源服务异常", "UpstreamError"))
    engine, metrics = make_engine(
        model, executor=executor, tool_failure_threshold=2
    )
    await collect(engine, "查号源")

    assert executor.calls == 2  # 第三次被工具级熔断短路
    counters = metrics.snapshot()["counters"]
    assert counters["tool_calls_total"] == 3
    assert counters["tool_calls_failed"] == 3


async def test_open_model_circuit_returns_rule_based_fallback():
    breaker = CircuitBreaker("fake", failure_threshold=1, recovery_timeout=100)
    breaker.record_failure()
    model = FakeChatModel([{"chunks": ["不该被调用"], "turn": ChatTurn(text="不该被调用")}])
    engine, metrics = make_engine(model, breaker=breaker)

    text, _ = await collect(engine, "我最近头痛")
    assert model.calls == 0
    assert "神经内科" in text
    assert "预约成功" not in text
    assert metrics.snapshot()["counters"]["degraded_total"] == 1


async def test_false_confirmation_falls_back_to_honest_answer_after_retry():
    """虚假承诺必须在**下发前**被截断，纠正重试后模型给出诚实回答。

    2026-11-10 事故的根因就是"事后追加免责提示"——提示挂在"预约成功"后面，
    用户先读到的仍然是"预约成功"。所以新契约是：假话根本不进入流式输出。
    """
    model = FakeChatModel(
        [
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            {"chunks": ["抱歉，我刚才没有真正执行预约。"], "turn": ChatTurn(text="抱歉，我刚才没有真正执行预约。")},
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine)

    assert "已为您预约成功" not in text  # 假话没被下发
    assert "抱歉，我刚才没有真正执行预约。" in text  # 纠正后是诚实回答
    assert FALSE_CONFIRMATION_NOTICE not in text
    counters = metrics.snapshot()["counters"]
    assert counters["false_confirmation_retries"] == 1
    assert counters["hallucination_suspected"] == 1


async def test_false_confirmation_corrective_retry_forces_real_tool_call():
    """真实前端联调（2026-11-10）暴露的场景：模型口头说"预约挂号中"却不调工具，
    下一轮还编造"预约挂号结果显示成功"。纠正重试应逼出真正的工具调用。"""
    model = FakeChatModel(
        [
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            # 收到纠正提示后，模型真正发起工具调用
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["预约手续已完成"], "turn": ChatTurn(text="预约手续已完成")},
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert "已为您预约成功" not in text  # 第一轮假话在下发前就被截断
    assert "预约手续已完成" in text  # 纠正后真正执行了工具
    assert FALSE_CONFIRMATION_NOTICE not in text  # 纠正成功 → 不再追加免责提示
    counters = metrics.snapshot()["counters"]
    assert counters["false_confirmation_retries"] == 1
    assert counters["hallucination_suspected"] == 1


async def test_corrective_retry_still_false_then_fallback_replaces_text():
    """纠正重试后模型仍编造成功 → 整段作废，换成陈述事实的兜底文案。

    注意：不是"追加"免责提示（那样用户仍会先读到"预约成功"），
    而是**替换**成"没有办理成功"的诚实结论。
    """
    model = FakeChatModel(
        [
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            {
                "chunks": ["真的，您的挂号已经预约好了"],
                "turn": ChatTurn(text="真的，您的挂号已经预约好了"),
            },
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert "已为您预约成功" not in text
    assert "预约好了" not in text
    assert FALLBACK_BOOKING_NOT_DONE in text
    assert FALSE_CONFIRMATION_NOTICE not in text
    counters = metrics.snapshot()["counters"]
    assert counters["false_confirmation_retries"] == 1
    assert counters["hallucination_suspected"] == 2  # 两轮都被判定
    assert counters["hallucination_fallbacks"] == 1


async def test_prior_booking_in_history_suppresses_notice_and_retry():
    """上一个请求里真的挂过号（有成功标记）→ "已经预约好了"是真话，不应误报也不应纠正重试"""
    history = [
        Message(
            role=Role.ASSISTANT,
            content="预约成功",
            tool_calls=[ToolCall(id="9", name="预约挂号", arguments={})],
            metadata={"booking_executed": True},
        )
    ]
    model = FakeChatModel(
        [
            {
                "chunks": ["您的挂号已经预约好了"],
                "turn": ChatTurn(text="您的挂号已经预约好了"),
            }
        ]
    )
    engine, metrics = make_engine(model, store=FakeStore(history))
    text, _ = await collect(engine, "挂号成功了吗？")

    assert "已经预约好了" in text
    assert FALSE_CONFIRMATION_NOTICE not in text
    assert model.calls == 1  # 没有纠正重试
    assert metrics.snapshot()["counters"]["hallucination_suspected"] == 0


async def test_cancelled_booking_in_history_still_flags_false_confirmation():
    """取消过号之后再说"已经预约好了" → 仍然是幻觉"""
    history = [
        Message(
            role=Role.ASSISTANT,
            content="预约成功",
            tool_calls=[ToolCall(id="9", name="预约挂号", arguments={})],
            metadata={"booking_executed": True},
        ),
        Message(
            role=Role.ASSISTANT,
            content="已取消",
            tool_calls=[ToolCall(id="10", name="取消预约挂号", arguments={})],
            metadata={"cancel_executed": True},
        ),
    ]
    model = FakeChatModel(
        [
            {
                "chunks": ["您的挂号已经预约好了"],
                "turn": ChatTurn(text="您的挂号已经预约好了"),
            },
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="11", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["已重新预约"], "turn": ChatTurn(text="已重新预约")},
        ]
    )
    engine, metrics = make_engine(model, store=FakeStore(history))
    text, _ = await collect(engine, "挂号成功了吗？")

    assert "预约好了" not in text  # 取消过号后再说"已经预约好了"仍是幻觉 → 截断
    assert "已重新预约" in text  # 纠正重试后真正调用了工具
    assert FALSE_CONFIRMATION_NOTICE not in text  # 纠正重试成功
    assert metrics.snapshot()["counters"]["false_confirmation_retries"] == 1


async def test_false_confirmation_is_not_partially_leaked():
    """回看窗口：承诺的**开头几个字**也不能漏给用户。

    "已为您预约成功"要等到"预约"两个字出现才命中正则，起点却在更早的"已"。
    没有回看窗口就会先漏出"已"再被截断 —— 用户看到半截承诺。
    """
    model = FakeChatModel(
        [
            {
                # 逐字下发，模拟真实流式的分块边界
                "chunks": ["好", "的", "，", "我", "已", "为", "您", "预", "约", "成", "功"],
                "turn": ChatTurn(text="好的，我已为您预约成功"),
            },
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["已受理"], "turn": ChatTurn(text="已受理")},
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert "预约成功" not in text
    assert "我已为您预约" not in text
    assert "已受理" in text


async def test_long_prefix_before_claim_is_kept():
    """承诺之前的正常内容应当保留（不能整段丢掉）"""
    prefix = "好的，我已经仔细核对过您的全部信息，包括姓名、身份证号、就诊日期和时段，"
    model = FakeChatModel(
        [
            {
                "chunks": [prefix, "已为您预约成功"],
                "turn": ChatTurn(text=prefix + "已为您预约成功"),
            },
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["已受理"], "turn": ChatTurn(text="已受理")},
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert "已经仔细核对过您的全部信息" in text
    assert "预约成功" not in text


async def test_truncated_prefix_is_trimmed_to_sentence_boundary():
    """截断后不能留下半截句子（"…上午" + 兜底 → 读起来像病句）"""
    model = FakeChatModel(
        [
            {
                "chunks": [
                    "很遗憾，您在2026年11月11日上午",
                    "已为您预约成功，请按时就诊。",
                ],
                "turn": ChatTurn(
                    text="很遗憾，您在2026年11月11日上午已为您预约成功，请按时就诊。"
                ),
            },
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})]
                )
            },
            {
                "chunks": ["抱歉，本次预约没有办理成功——系统没有生成对应的预约记录。"],
                "turn": ChatTurn(text="抱歉，本次预约没有办理成功——系统没有生成对应的预约记录。"),
            },
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert "预约成功" not in text
    assert "上午" not in text  # 半截句子不能留在正文里
    # 纠正重试后模型给出了诚实答复（不是引擎兜底文案）
    assert "没有办理成功" in text


async def test_textual_tool_call_is_intercepted_before_user():
    """模型把工具调用写成纯文本 JSON → 原始参数不能漏给用户，且要纠正重试。

    实测（qwen2.5:7b）：模型不发出结构化 tool_call，而是把
    `预约挂号 {"username": "张三", ...}` 当正文吐出来 ——
    用户看到一坨 JSON，而且工具一次都没执行。
    """
    json_text = (
        '预约挂号 {"username": "张三", "id_card": "110101199005054322",'
        ' "department": "神经内科", "date": "2026-11-20", "time": "上午"}'
    )
    model = FakeChatModel(
        [
            {"chunks": [json_text], "turn": ChatTurn(text=json_text)},
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["已受理"], "turn": ChatTurn(text="已受理")},
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    assert "id_card" not in text  # 原始参数不能漏给用户
    assert "110101199005054322" not in text
    assert "已受理" in text
    counters = metrics.snapshot()["counters"]
    assert counters["textual_tool_call_truncated"] == 1


async def test_narrated_tool_call_triggers_corrective_retry():
    """模型只说"我将调用预约挂号工具"却没真调 → 必须逼它真的调一次。

    2026-11-10 真实前端联调实测：用户看到一句"我将调用预约挂号工具"，
    然后什么都没发生，工具一次都没执行（trace tool_call_count=0）。
    """
    model = FakeChatModel(
        [
            {
                "chunks": ["好的，我将为您提交预约请求。请稍等，我将调用预约挂号工具。"],
                "turn": ChatTurn(
                    text="好的，我将为您提交预约请求。请稍等，我将调用预约挂号工具。"
                ),
            },
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="1", name="预约挂号", arguments={})]
                )
            },
            {"chunks": ["预约已办好"], "turn": ChatTurn(text="预约已办好")},
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    counters = metrics.snapshot()["counters"]
    assert counters["narrated_tool_call_retries"] == 1
    assert "预约已办好" in text


async def test_narrated_tool_call_only_retried_once():
    """纠正重试后仍只是口头承诺 → 不再无限重试，保留模型的说明文字"""
    model = FakeChatModel(
        [
            {
                "chunks": ["我这就调用预约挂号工具。"],
                "turn": ChatTurn(text="我这就调用预约挂号工具。"),
            },
            {
                "chunks": ["我还是调用预约挂号工具。"],
                "turn": ChatTurn(text="我还是调用预约挂号工具。"),
            },
        ]
    )
    engine, metrics = make_engine(model)
    text, _ = await collect(engine, "帮我预约神经内科")

    counters = metrics.snapshot()["counters"]
    assert counters["narrated_tool_call_retries"] == 1
    assert model.calls == 2  # 只重试了一轮
    assert "调用预约挂号工具" in text


async def test_request_metrics_are_recorded():
    model = FakeChatModel([{"chunks": ["好的"], "turn": ChatTurn(text="好的")}])
    engine, metrics = make_engine(model)
    await collect(engine)

    snapshot = metrics.snapshot()
    assert snapshot["counters"]["requests_total"] == 1
    assert snapshot["counters"]["llm_calls_total"] == 1
    assert snapshot["histograms"]["request_duration_ms"]["count"] == 1


async def test_tool_results_are_persisted_with_assistant_message():
    """落库必须包含工具结果消息 —— 此前只存 assistant(tool_calls)，
    下一请求的历史里"发起了调用"却找不到对应结果，序列残缺
    （2026-11-10 审计发现）。"""
    model = FakeChatModel(
        [
            {
                "turn": ChatTurn(
                    tool_calls=[
                        ToolCall(id="t1", name="查询是否有号源", arguments={})
                    ]
                )
            },
            {"chunks": ["查到了"], "turn": ChatTurn(text="查到了")},
        ]
    )
    store = FakeStore()
    engine, _ = make_engine(model, store=store)
    await collect(engine, "帮我查号源")

    saved = store.saved
    assert any(m.role == Role.TOOL for m in saved), "工具结果消息没有落库"
    assistant_idx = next(i for i, m in enumerate(saved) if m.tool_calls)
    tool_idx = next(i for i, m in enumerate(saved) if m.role == Role.TOOL)
    assert tool_idx == assistant_idx + 1  # 结果紧跟发起调用的 assistant
    assert saved[tool_idx].tool_call_id == saved[assistant_idx].tool_calls[0].id


async def test_narrative_and_final_reply_are_persisted_separately():
    """叙述段与最终回复拆开落库（2026-11-10 第七轮事故）。

    旧逻辑把所有轮次文本合并进一条 assistant content，
    "请稍等，我正在查询…"的叙述句进了历史，模型下一轮**原样照抄**
    该句式且零工具调用。新结构：assistant(tool_calls,叙述段) → tool →
    assistant(最终回复)，与真实对话流一致。
    """
    narrative = "好的，我将为您预约内科。请稍等，我正在查询是否有可用号源。"
    model = FakeChatModel(
        [
            {
                "chunks": [narrative],
                "turn": ChatTurn(
                    text=narrative,
                    tool_calls=[
                        ToolCall(id="t7", name="查询是否有号源", arguments={})
                    ],
                ),
            },
            {"chunks": ["本院没有内科，请换科室"], "turn": ChatTurn(text="本院没有内科，请换科室")},
        ]
    )
    store = FakeStore()
    engine, _ = make_engine(model, store=store)
    await collect(engine, "王新勃，610121199401032831，内科，2026.11.11")

    saved = store.saved
    assistant_idx = next(i for i, m in enumerate(saved) if m.tool_calls)
    tool_idx = next(i for i, m in enumerate(saved) if m.role == Role.TOOL)
    # 叙述段独立保留在 assistant(tool_calls) 里
    assert "正在查询" in saved[assistant_idx].content
    # 最终回复是 tool 结果之后的独立 assistant 消息，不含叙述句
    closing = saved[tool_idx + 1]
    assert closing.role == Role.ASSISTANT
    assert not closing.tool_calls
    assert closing.content == "本院没有内科，请换科室"
    assert "正在查询" not in closing.content


async def test_persisted_history_is_complete_sequence():
    """落库序列完整性：每个 assistant(tool_calls) 的每个调用都有配对结果"""
    model = FakeChatModel(
        [
            {
                "turn": ChatTurn(
                    tool_calls=[
                        ToolCall(id="t1", name="查询是否有号源", arguments={}),
                        ToolCall(id="t2", name="查询科室专家", arguments={}),
                    ]
                )
            },
            {"chunks": ["都查到了"], "turn": ChatTurn(text="都查到了")},
        ]
    )
    store = FakeStore()
    engine, _ = make_engine(model, store=store)
    await collect(engine, "查号源和专家")

    saved = store.saved
    call_ids = {c.id for m in saved if m.tool_calls for c in m.tool_calls}
    result_ids = {
        m.tool_call_id for m in saved if m.role == Role.TOOL
    }
    assert {"t1", "t2"} <= call_ids
    assert {"t1", "t2"} <= result_ids


# ---- 事件补全（核心模块第 3 批 T11） ----


async def test_retrieval_event_emitted_when_knowledge_injected():
    """知识注入时必须发出结构化 RetrievalEvent（此前只进 trace）"""

    class FakeHit:
        text = "神经内科知识"
        score = 0.9
        metadata = {"heading_path": "科室/神经内科"}

    class FakeRetriever:
        async def retrieve(self, text, top_k=2):
            return [FakeHit()]

    engine, _ = make_engine(
        FakeChatModel([{"chunks": ["好的"], "turn": ChatTurn(text="好的")}]),
        retriever=FakeRetriever(),
    )
    _, events = await collect(engine, "头痛挂什么科")
    retrieval = [e for e in events if e.event_type == EventType.RETRIEVAL]
    assert len(retrieval) == 1
    assert retrieval[0].metadata["hit_count"] == 1
    assert retrieval[0].metadata["top_heading"] == "科室/神经内科"


async def test_tool_result_event_carries_preview_and_duration():
    model = FakeChatModel(
        [
            {
                "turn": ChatTurn(
                    tool_calls=[ToolCall(id="t1", name="查询是否有号源", arguments={})]
                )
            },
            {"chunks": ["查到了"], "turn": ChatTurn(text="查到了")},
        ]
    )
    engine, _ = make_engine(model, executor=FakeExecutor(ToolResult.ok("神经内科有号源")))
    _, events = await collect(engine, "查号源")
    results = [e for e in events if e.event_type == EventType.TOOL_RESULT]
    assert results
    assert results[0].metadata["result_preview"] == "神经内科有号源"
    assert "duration_ms" in results[0].metadata


async def test_claim_truncation_and_fallback_emit_quality_events():
    """谎话截断与兜底替换都要发结构化事件 —— events 通道能看到质量防线动作"""
    model = FakeChatModel(
        [
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            {
                "chunks": ["真的，您的挂号已经预约好了"],
                "turn": ChatTurn(text="真的，您的挂号已经预约好了"),
            },
        ]
    )
    engine, _ = make_engine(model)
    _, events = await collect(engine, "帮我预约神经内科")

    degraded = [e for e in events if e.event_type == EventType.DEGRADED]
    reasons = [e.metadata.get("reason") for e in degraded]
    assert "claim_truncated" in reasons
    assert "hallucination_fallback" in reasons


async def test_agent_end_reports_corrective_retries():
    model = FakeChatModel(
        [
            {"chunks": ["已为您预约成功"], "turn": ChatTurn(text="已为您预约成功")},
            {"chunks": ["抱歉，没有真正执行。"], "turn": ChatTurn(text="抱歉，没有真正执行。")},
        ]
    )
    engine, _ = make_engine(model)
    _, events = await collect(engine, "帮我预约神经内科")
    end = [e for e in events if e.event_type == EventType.AGENT_END][0]
    assert end.metadata["corrective_retries"] == 1


@pytest.mark.asyncio
async def test_corrective_retry_does_not_merge_rounds_into_history():
    """纠正重试后落库只保留最后一轮回复，叙述承诺不得进历史。

    2026-11-10 第十轮事故（memoryId 287312 [19]）：轮1输出叙述承诺被门控
    拦截 → 轮2裸 tool_call（空文本）→ 轮3最终回复。result.text 跨轮累积，
    落库把轮1+轮3拼成一条 assistant content，历史里留下"承诺+回答"的
    拼接文本，用户端也看到两段回复无缝粘在一起。
    """
    narration = "好的，我将为您预约心脏外科的李四医生。请稍等，我将为您完成预约。"
    final = "您好，本院没有心脏外科这个科室。"
    model = FakeChatModel(
        [
            {"chunks": [narration], "turn": ChatTurn(text=narration)},
            {
                "chunks": [],
                "turn": ChatTurn(
                    text="",
                    tool_calls=[
                        ToolCall(id="c1", name="预约挂号", arguments={"username": "李四"})
                    ],
                ),
            },
            {"chunks": [final], "turn": ChatTurn(text=final)},
        ]
    )

    class FailExecutor:
        async def execute(self, call, tool):
            return ToolResult.fail(
                "参数校验未通过：本院没有【心脏外科】", error_type="ValidationError"
            )

    engine, _ = make_engine(model, executor=FailExecutor())
    await collect(engine, "我要预约的医生是李四")

    saved = engine.conversation_store.saved
    joined = "".join((m.content or "") for m in saved)
    assert narration not in joined, "叙述承诺文本泄漏进了历史"
    closing = [m for m in saved if m.role == Role.ASSISTANT and not m.tool_calls]
    assert closing, "最终回复没有落库"
    assert closing[-1].content == final, "落库的最终回复应只含最后一轮文本"


# ---- Phase 3a 真机回归（2026-11-11）：审批兜底不得误伤"执行后转述轮" ----

async def test_approval_forced_retry_skipped_when_tools_already_executed():
    """带审批 token + 工具已执行 → 模型转述轮不再被强制重试。

    2026-11-11 8082 真机实测：执行轮正常时序是"消费 token → 调工具
    （返回 ALREADY_EXISTS）→ 模型转述结果"。旧判据只看「本轮没有
    tool_calls」，把转述轮误判成"token 白拿"→ 强制重试一轮 →
    同一句在界面上重复输出两遍。
    """
    from xiaozhi.security.approval_service import (
        ApprovalContext,
        reset_approval_context,
        set_approval_context,
    )

    set_approval_context(ApprovalContext(approval_token="t0k", principal=None))
    try:
        final = "您在呼吸内科 2026-11-12 上午已有预约，请勿重复预约。"
        model = FakeChatModel(
            [
                {
                    "chunks": [],
                    "turn": ChatTurn(
                        text="",
                        tool_calls=[ToolCall(id="e1", name="预约挂号", arguments={})],
                    ),
                },
                {"chunks": [final], "turn": ChatTurn(text=final)},
            ]
        )
        engine, metrics = make_engine(model)
        text, _events = await collect(engine, "帮我预约呼吸内科")

        assert model.calls == 2, (
            f"执行轮+转述轮共 2 次模型调用，实际 {model.calls} —— "
            "转述轮被误判触发了强制重试"
        )
        assert text.count("已有预约") == 1, "转述文本被重复下发"
        assert metrics.snapshot()["counters"]["llm_calls_total"] == 2
    finally:
        reset_approval_context()


async def test_approval_forced_retry_still_fires_on_zero_tool_calls():
    """带 token 但全程零工具调用 → 强制重试兜底仍然生效（第十轮事故行为保持）。"""
    from xiaozhi.security.approval_service import (
        ApprovalContext,
        reset_approval_context,
        set_approval_context,
    )

    set_approval_context(ApprovalContext(approval_token="t0k", principal=None))
    try:
        model = FakeChatModel(
            [
                {"chunks": ["请在确认窗口核对"], "turn": ChatTurn(text="请在确认窗口核对")},
                {"chunks": ["好的，马上办理"], "turn": ChatTurn(text="好的，马上办理")},
            ]
        )
        engine, _metrics = make_engine(model)
        text, _events = await collect(engine, "帮我预约")

        assert model.calls == 2, "带 token 却零工具调用，必须触发一次强制重试"
        assert "马上办理" in text
    finally:
        reset_approval_context()
