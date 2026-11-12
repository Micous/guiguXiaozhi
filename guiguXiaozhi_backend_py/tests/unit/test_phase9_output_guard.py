"""Phase 9-A：医疗回复出口扫描单测。

Phase 7 只做了入口拦截（越界请求不进模型）。这一层补的是**出口**：
正常请求但模型自己漏出用药/诊断。真机依据见
`踩坑与优化记录.md` 22.4（"可以使用一些非处方药物，如感冒药、退热药"）。

三条要守住的性质：
  1. 命中就丢句，且整段命中时换成固定话术（不能只剩一句"我不能给建议"）；
  2. 我们自己的合规话术**不能**被自己拦掉（带否定词的表述一律放行）；
  3. 流式仍然流式：按句放行，不整段扣住（7B 跑一分钟，整段扣住＝白屏）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from xiaozhi.ops.metrics import MetricsCollector  # noqa: E402
from xiaozhi.reliability.resilience import RetryConfig  # noqa: E402
from xiaozhi.security.output_guard import (  # noqa: E402
    DIAGNOSIS,
    MEDICATION,
    OUTPUT_GUARD_REPLY,
    OUTPUT_GUARD_SUFFIX,
    MedicalOutputGuard,
    scan_medical_output,
    strip_blocked_sentences,
)

from test_engine_characterization import make_engine  # noqa: E402
from test_engine_reliability import ChatTurn, FakeChatModel, collect  # noqa: E402


# ==========================================================================
# 1. 扫描规则
# ==========================================================================


@pytest.mark.parametrize(
    "text,kind",
    [
        ("可以吃点布洛芬。", MEDICATION),
        ("建议服用阿莫西林胶囊。", MEDICATION),
        ("可以使用一些非处方药物缓解症状。", MEDICATION),
        ("如感冒药、退热药等。", MEDICATION),
        ("每次一片，一天三次。", MEDICATION),
        ("剂量是 0.3g，每日两次。", MEDICATION),
        ("可以先吃点消炎药。", MEDICATION),
        ("这个情况大概率是上呼吸道感染。", DIAGNOSIS),
        ("可以排除肺炎。", DIAGNOSIS),
        ("您这是急性咽炎。", DIAGNOSIS),
        ("不用去医院，在家观察就行。", DIAGNOSIS),
        ("您的化验结果正常。", DIAGNOSIS),
    ],
)
def test_violations_are_detected(text, kind):
    verdict = scan_medical_output(text)
    assert verdict.hit, text
    assert verdict.kind == kind
    assert verdict.rule_id  # 必须说得出是哪条规则（进日志/指标）


@pytest.mark.parametrize(
    "text",
    [
        # 正常咨询口径：允许（方案 A 允许解释公开常识 + 分诊引导）
        "建议您多休息、多喝水，注意保暖。",
        "这种情况建议到呼吸内科门诊就诊，由医生当面评估。",
        "我可以帮您预约呼吸内科的门诊。",
        "已为您预约 2026-11-21 上午呼吸内科门诊。",
        "医院门诊时间是上午 8:00 到下午 17:00。",
        "抽血检查一般需要空腹 8 小时。",
        # 我们自己的合规话术：带否定词，绝不能拦
        "我不能推荐药品或用药剂量。",
        "不要自行服用任何药物，用药请咨询药师。",
        "不要喂水、喂药，等急救人员到场。",
        "请勿自行调整药量，需由医生评估。",
        "不能判断检查结果是否正常。",
    ],
)
def test_legit_texts_are_not_blocked(text):
    assert not scan_medical_output(text).hit, text


def test_empty_text_passes():
    assert not scan_medical_output("").hit


# ==========================================================================
# 2. 流式护栏
# ==========================================================================


def test_blocked_sentence_is_dropped_and_suffix_added():
    guard = MedicalOutputGuard()
    out = guard.feed("建议多休息。可以吃点布洛芬缓解。") + guard.flush()
    assert "布洛芬" not in out
    assert "建议多休息。" in out
    assert OUTPUT_GUARD_SUFFIX.strip() in out
    assert guard.blocked_any
    assert guard.blocked_rules[0].startswith("medication.drug")


def test_all_blocked_falls_back_to_fixed_reply():
    """整段都违规时不能让用户只看到一句"我不能给建议"。"""
    guard = MedicalOutputGuard()
    out = guard.feed("建议服用布洛芬。") + guard.feed("每次一片。") + guard.flush()
    assert out == OUTPUT_GUARD_REPLY
    assert "布洛芬" not in out
    assert "一次一片" not in out


def test_suffix_is_emitted_only_once():
    guard = MedicalOutputGuard()
    out = (
        guard.feed("建议服用布洛芬。")
        + guard.feed("多喝热水。")
        + guard.feed("每次一片。")
        + guard.flush()
    )
    assert out.count("涉及具体用药与诊断判断") == 1
    assert "多喝热水。" in out


def test_streaming_holds_back_until_sentence_boundary():
    """按句放行：半句话不能先漏出去（否则用户会先看到"布洛"再被吞掉）。"""
    guard = MedicalOutputGuard()
    assert guard.feed("可以吃点布") == ""
    assert guard.feed("洛芬。") == ""
    assert "布洛芬" not in guard.flush()


def test_streaming_splits_sentences_across_chunks():
    guard = MedicalOutputGuard()
    parts = [
        guard.feed("建议多休息、多喝水。"),
        guard.feed("如果发烧可以吃退烧药。"),
        guard.feed("症状加重请及时就诊。"),
    ]
    out = "".join(parts) + guard.flush()
    assert "多休息" in out
    assert "退烧药" not in out
    assert "及时就诊" in out


def test_long_sentence_without_punctuation_is_released():
    """模型偶发输出一大段无标点文本：不能一直扣着（首字延迟会失控）。"""
    guard = MedicalOutputGuard(max_hold=20)
    text = "这是一段没有句末标点但内容完全正常的说明文字，" * 3
    out = guard.feed(text)
    assert out, "超过 max_hold 必须放行一部分，否则前端一直白屏"
    assert len(out) >= 10


def test_disabled_guard_is_transparent():
    guard = MedicalOutputGuard(enabled=False)
    assert guard.feed("可以吃点布洛芬。") == "可以吃点布洛芬。"
    assert guard.flush() == ""


def test_metrics_are_recorded():
    metrics = MetricsCollector()
    guard = MedicalOutputGuard(metrics=metrics)
    guard.feed("建议多休息。可以吃点布洛芬。")
    guard.flush()
    counters = metrics.snapshot()["counters"]
    assert counters.get("medical_output_hits_total") == 1
    # 部分命中 → 追加说明（rewritten）；整段命中 → 换成固定话术（blocked）
    assert counters.get("medical_output_rewritten_total") == 1

    metrics2 = MetricsCollector()
    guard2 = MedicalOutputGuard(metrics=metrics2)
    guard2.feed("可以吃点布洛芬。")
    guard2.flush()
    assert metrics2.snapshot()["counters"].get("medical_output_blocked_total") == 1


def test_strip_blocked_sentences_keeps_narrative_clean():
    text = "建议多休息。可以吃点布洛芬。症状加重请及时就诊。"
    assert strip_blocked_sentences(text) == "建议多休息。症状加重请及时就诊。"


def test_strip_blocked_sentences_handles_trailing_text():
    assert strip_blocked_sentences("建议服用阿莫西林") == ""


# ==========================================================================
# 3. 引擎级：用户看到的与落库的都不能带用药建议
# ==========================================================================


async def test_engine_drops_medication_advice_from_stream_and_history(monkeypatch):
    reply = "建议多休息、多喝水。可以吃点布洛芬缓解。症状加重请及时就诊。"
    model = FakeChatModel(
        [{"chunks": [reply[i : i + 7] for i in range(0, len(reply), 7)], "turn": ChatTurn(text=reply)}]
    )
    engine, _ = make_engine(model, retry_config=RetryConfig(max_attempts=1))
    text, _ = await collect(engine, "感冒了怎么办")

    assert "布洛芬" not in text
    assert "多休息" in text and "及时就诊" in text
    assert "涉及具体用药与诊断判断" in text
    # 落库的历史同样不能带（叙述段/最终回复都会进 Mongo）
    dumped = "".join(m.content or "" for m in engine.conversation_store.saved)
    assert "布洛芬" not in dumped


async def test_engine_keeps_normal_consultation_untouched():
    reply = "建议您多休息、多喝水，注意保暖。如症状加重请到呼吸内科门诊就诊。"
    model = FakeChatModel([{"chunks": [reply], "turn": ChatTurn(text=reply)}])
    engine, _ = make_engine(model, retry_config=RetryConfig(max_attempts=1))
    text, _ = await collect(engine, "感冒了怎么办")
    assert text == reply
    assert OUTPUT_GUARD_SUFFIX.strip() not in text


async def test_engine_booking_reply_is_unaffected():
    """办理类回复（预约成功）不能因为出口扫描被改写。"""
    from xiaozhi.core.message import ToolCall

    reply = "已为您预约呼吸内科 2026-11-21 上午的门诊，请按时就诊。"
    model = FakeChatModel(
        [
            {
                "chunks": [],
                "turn": ChatTurn(
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="call-1",
                            name="预约挂号",
                            arguments={"department": "呼吸内科", "date": "2026-11-21"},
                        )
                    ],
                ),
            },
            {"chunks": [reply], "turn": ChatTurn(text=reply)},
        ]
    )
    engine, _ = make_engine(model, retry_config=RetryConfig(max_attempts=1))
    text, _ = await collect(engine, "帮我预约呼吸内科 2026-11-21 上午")
    assert "已为您预约" in text
    assert OUTPUT_GUARD_SUFFIX.strip() not in text
    assert text.strip() == reply


def test_dangling_continuation_is_dropped_with_previous_sentence():
    """真机撞到的形态：药名句被丢掉后，紧跟的"但记得要…"变成半截话。

    2026-11-12 真机（不带边界提示词）模型原始输出：
        "孩子发烧38.5度，可以先给孩子用点布洛芬混悬液或者对乙酰氨基酚，这两种药
         都是常用的退烧药。但记得要按照孩子的体重和年龄来确定剂量…"
    丢掉第一句后，用户看到的第一句就是"但记得要…"——指代对象已经没了。
    """
    guard = MedicalOutputGuard()
    out = (
        guard.feed("可以先给孩子用点布洛芬混悬液。")
        + guard.feed("但记得要按体重确定用量。")
        + guard.feed("同时注意多喝水。")
        + guard.flush()
    )
    assert "布洛芬" not in out
    assert "但记得" not in out
    assert "同时注意多喝水。" in out
    assert "guard.dangling_continuation" in guard.blocked_rules


def test_dangling_rule_only_applies_to_adversative_prefix():
    """不是所有后续句都丢：陈述事实的句子必须保留（否则会过度删减）。"""
    guard = MedicalOutputGuard()
    out = guard.feed("可以吃点布洛芬。") + guard.feed("医院门诊时间是 8:00 到 17:00。") + guard.flush()
    assert "门诊时间" in out


def test_physical_cooling_is_not_medication():
    """真机误伤：`可以先用物理降温，比如用温水擦浴` 被当成用药建议丢掉。

    根因是动词表里有泛动词 `用`（"可以先"+"用"）。泛动词的宾语几乎总是
    药名或药品类别，那两条规则已经覆盖，动词表只保留制药专用词。
    """
    for text in (
        "可以先用物理降温，比如用温水擦浴。",
        "可以先给孩子少穿点衣服散热。",
        "可以用加湿器保持室内湿度。",
    ):
        assert not scan_medical_output(text).hit, text
