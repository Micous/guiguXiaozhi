"""第九轮事故回归（2026-11-11）：「预约挂号没有审批弹窗」。

真实前端联调（用户报"预约挂号，没有审批弹窗"）实测到的现象：

  轮1 用户：「预约挂号」 → 模型追问姓名/身份证/科室/日期/时段
  轮2 用户：「王新勃，610121199401032831，内科，2026.11.12」
  轮2 模型：「好的，我将为您预约内科。内科在本院开设的科室中并不具体指代某个科室，
           根据您的需求，我将为您推荐呼吸内科。请问您是否同意预约呼吸内科？」
           「好的，正在为您预约呼吸内科的号，请稍等片刻。预约信息如下：……
            请确认无误后点击确认按钮完成预约。」

**零工具调用**。于是：

  1. 写操作根本没进审批门控 → 后端不下发审批 sentinel → **前端没有确认弹窗**；
     用户的实际诉求（挂号）也**没有发生**（数据库无新增记录）。
  2. 模型还编出"点击确认按钮"——系统里根本没有那个按钮（确认窗只有前端
     审批弹窗才渲染），用户照着找会落空。

根因：``_NARRATED_TOOL_CALL`` 的四个分支**全部漏判**这两种句式。
  * 旧分支要求动词后必须跟固定「业务名词表」里的词（科室/号源/专家…），
    而模型说的是"预约**内科**"、"预约呼吸内科**的号**"——不在表里；
  * 宾语是**科室名**（呼吸内科/神经内科…）时，表里的「科室」匹配不到，
    因为模型写的是简称而不是"某某科室"。

修复后：叙述承诺被识别 → 纠正重试触发 → 模型**真的调工具** → 写操作进审批门控
→ 后端下发 sentinel → 前端弹确认框。

本文件钉死三件事：
  A. 叙述检测必须命中模型实际会吐的句式（枚举"模型实际会吐什么"）；
  B. 纠正重试必须被触发（叙述轮 → 工具轮 → 审批门控）；
  C. 教学/陈述/泛化推荐句式不能被误伤（否则会把正常回答拦腰打断）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from xiaozhi.core.message import ToolCall  # noqa: E402
from xiaozhi.core.tool import ToolResult  # noqa: E402
from xiaozhi.models.ollama import ChatTurn  # noqa: E402

from test_engine_characterization import make_engine  # noqa: E402
from test_engine_reliability import FakeChatModel, FakeExecutor, collect  # noqa: E402

# ---- 事故原句（逐字取自真实联调输出） ----
NARRATION_A = (
    "好的，我将为您预约内科。内科在本院开设的科室中并不具体指代某个科室，"
    "根据您的需求，我将为您推荐呼吸内科。请问您是否同意预约呼吸内科？"
)
NARRATION_B = (
    "好的，正在为您预约呼吸内科的号，请稍等片刻。"
    "预约信息如下：姓名：王新勃，身份证号：610121199401032831，"
    "科室：呼吸内科，日期：2026-11-24，时段：上午。"
    "请确认无误后点击确认按钮完成预约。"
)


# ---------- A. 叙述检测：命中模型实际句式 ----------


def test_narrated_department_promise_is_detected():
    """模型说"我将为您预约内科"/"正在为您预约呼吸内科的号"必须被识别。

    旧正则四个分支全漏判 → 纠正重试不触发 → 零工具调用 → 无审批弹窗。
    """
    from xiaozhi.models.quality import detect_narrated_tool_call

    assert detect_narrated_tool_call(NARRATION_A)
    assert detect_narrated_tool_call(NARRATION_B)


def test_narrated_department_promise_variants_are_detected():
    """同一语义的其他换皮句式也要拦住（枚举"模型实际会吐什么"）。"""
    from xiaozhi.models.quality import detect_narrated_tool_call

    assert detect_narrated_tool_call("我将为您预约神经内科。")
    assert detect_narrated_tool_call("我正在为您办理挂号。")
    assert detect_narrated_tool_call("稍后为您取消预约。")
    assert detect_narrated_tool_call("正在为您预约骨科的号。")
    assert detect_narrated_tool_call("我这就为您挂号外科。")


def test_narrated_detection_does_not_break_teaching_or_statements():
    """教学/陈述/泛化推荐句式不能被误伤 —— 否则会把正常回答拦腰打断。"""
    from xiaozhi.models.quality import detect_narrated_tool_call

    # 教学句式：解释流程，不是承诺执行
    assert not detect_narrated_tool_call("我将为您介绍预约挂号的流程。")
    assert not detect_narrated_tool_call("我为您讲解一下如何查询就诊记录。")
    assert not detect_narrated_tool_call("我可以为您总结预约挂号的三种方式。")
    # 陈述事实
    assert not detect_narrated_tool_call("本院预约放号时间为每天早上7点。")
    assert not detect_narrated_tool_call("建议您挂神经内科，注意休息。")
    # 泛化推荐（非挂号业务对象）
    assert not detect_narrated_tool_call("我可以为您推荐一些日常保健的注意事项。")
    # 知识问答
    assert not detect_narrated_tool_call("呼吸内科主要治疗呼吸系统疾病，通常上午号源较多。")


# ---------- B. 纠正重试被触发 → 真的调工具 ----------


async def test_narrated_department_promise_triggers_corrective_retry():
    """叙述承诺（零工具调用）→ 纠正重试 → 模型真的调工具。

    没有这一步，写操作就永远进不了审批门控，用户等不到确认弹窗。
    """
    model = FakeChatModel(
        [
            {"chunks": [NARRATION_A], "turn": ChatTurn(text=NARRATION_A)},
            # 纠正轮：模型被逼真调工具
            {
                "turn": ChatTurn(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="预约挂号",
                            arguments={
                                "username": "王新勃",
                                "id_card": "610121199401032831",
                                "department": "呼吸内科",
                                "date": "2026-11-24",
                                "time": "上午",
                            },
                        )
                    ]
                )
            },
        ]
    )
    executor = FakeExecutor(ToolResult.ok("预约成功"))
    engine, metrics = make_engine(model, executor=executor)
    await collect(engine, "帮我预约呼吸内科")

    # 模型调用次数 = 叙述轮 + 纠正轮（真调工具）+ 工具轮后的总结轮
    assert model.calls >= 2, "叙述承诺后必须补一轮，让模型真的调工具"
    assert metrics.snapshot()["counters"]["narrated_tool_call_retries"] == 1
    assert executor.calls == 1, "工具必须真的被执行（进审批门控）"


async def test_narrated_promise_streamed_char_by_char_still_detected():
    """叙述句逐字跨 chunk 到达时同样要触发纠正重试（流式场景）。"""
    from xiaozhi.models.quality import detect_narrated_tool_call

    text = "好的，我将为您预约呼吸内科。"
    # 逐字喂入，累计文本最终仍应命中
    assert detect_narrated_tool_call(text)

    model = FakeChatModel(
        [
            {
                "chunks": list(text),
                "turn": ChatTurn(text=text),
            },
            {
                "turn": ChatTurn(
                    tool_calls=[
                        ToolCall(
                            id="c1",
                            name="预约挂号",
                            arguments={
                                "username": "王新勃",
                                "id_card": "610121199401032831",
                                "department": "呼吸内科",
                                "date": "2026-11-24",
                                "time": "上午",
                            },
                        )
                    ]
                )
            },
        ]
    )
    executor = FakeExecutor(ToolResult.ok("预约成功"))
    engine, metrics = make_engine(model, executor=executor)
    await collect(engine, "帮我预约呼吸内科")

    assert metrics.snapshot()["counters"]["narrated_tool_call_retries"] == 1
    assert executor.calls == 1


# ---------- C. 审批说明注入：消除"点击确认按钮"幻觉 ----------


def test_approval_notice_is_injected_only_when_approval_enabled():
    """审批开启 → 告诉模型"确认窗由系统弹出"；关闭 → 提示词与一期完全一致。

    不注入的话模型会编出"请确认无误后点击确认按钮完成预约"——
    系统里根本没有那个按钮，而且它把确认当成自己的事就不真调工具，
    写操作永远进不了审批门控（2026-11-11 真实联调事故）。
    """
    from xiaozhi.runtime.engine_phases import APPROVAL_PROMPT_NOTE

    executor = FakeExecutor(ToolResult.ok("OK"))
    engine, _ = make_engine(FakeChatModel([]), executor=executor)
    # 灰度关闭：执行器上没有 approval_service
    assert engine._approval_enabled() is False

    class _ApprovalExecutor(FakeExecutor):
        def __init__(self):
            super().__init__(ToolResult.ok("OK"))
            self.approval_service = object()

    engine2, _ = make_engine(FakeChatModel([]), executor=_ApprovalExecutor())
    assert engine2._approval_enabled() is True

    # 说明内容必须点明"由系统弹出确认窗"且"不要让用户点按钮"
    assert "确认窗" in APPROVAL_PROMPT_NOTE
    assert "不要让用户去点击任何按钮" in APPROVAL_PROMPT_NOTE


async def test_approval_notice_reaches_model_context_when_enabled():
    """审批开启时，说明文本必须真的进到发给模型的 system 提示里。"""
    from xiaozhi.runtime.engine_phases import APPROVAL_PROMPT_NOTE

    seen = {}

    class _CaptureAssembler:
        history = []
        dropped_items = 0
        total_tokens = 0

        def assemble(self, system_prompt, user_input, history, knowledge="", profile=""):
            seen["system_prompt"] = system_prompt
            return _CaptureContext()

    class _CaptureContext:
        history = []
        dropped_items = 0
        total_tokens = 0
        system_prompt = ""

        def to_messages(self, user_input, *, neutralize_history=False):
            return [{"role": "user", "content": user_input}]

    class _ApprovalExecutor(FakeExecutor):
        def __init__(self):
            super().__init__(ToolResult.ok("OK"))
            self.approval_service = object()

    model = FakeChatModel([{"chunks": ["好的"], "turn": ChatTurn(text="好的")}])
    engine, _ = make_engine(
        model, executor=_ApprovalExecutor(), assembler=_CaptureAssembler()
    )
    await collect(engine, "帮我预约呼吸内科")

    assert APPROVAL_PROMPT_NOTE in seen["system_prompt"]
