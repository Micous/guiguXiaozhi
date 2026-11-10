"""结构化日志与通用护栏单测。"""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xiaozhi.reliability.logging import (  # noqa: E402
    JsonFormatter,
    configure_logging,
    log_event,
    redact_text,
)
from xiaozhi.security.guardrails import check_input, check_output  # noqa: E402


def _record(message: str, **extra):
    record = logging.LogRecord("test", logging.INFO, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_redacts_message():
    payload = json.loads(
        JsonFormatter().format(
            _record("患者身份证 110101198805123456，密钥 sk-abcdef123456")
        )
    )
    assert payload["level"] == "INFO"
    assert "110101198805123456" not in payload["message"]
    assert "sk-abcdef123456" not in payload["message"]
    assert payload["message"].count("[REDACTED]") == 2


def test_json_formatter_redacts_extra_but_keeps_token_counts():
    record = _record(
        "tool ok",
        event="tool.execute",
        trace_id="t-1",
        fields={"id_card": "110101198805123456", "prompt_tokens": 128},
    )
    payload = json.loads(JsonFormatter().format(record))
    fields = payload["context"]["fields"]
    assert fields["id_card"] == "[REDACTED]"
    assert fields["prompt_tokens"] == 128  # token 计数不是凭证，不该打码
    assert payload["context"]["event"] == "tool.execute"


def test_configure_logging_is_idempotent():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        configure_logging("INFO", json_output=True, stream=io.StringIO())
        configure_logging("INFO", json_output=False, stream=io.StringIO())
        markers = [
            h for h in root.handlers if getattr(h, "_xiaozhi_json_handler", False)
        ]
        assert len(markers) == 1
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_log_event_writes_structured_payload():
    stream = io.StringIO()
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        configure_logging("INFO", json_output=True, stream=stream)
        log_event(
            logging.getLogger("unit"),
            logging.INFO,
            "tool.execute",
            trace_id="t-2",
            id_card="110101198805123456",
        )
        payload = json.loads(stream.getvalue().strip().splitlines()[-1])
        assert payload["context"]["event"] == "tool.execute"
        assert payload["context"]["fields"]["id_card"] == "[REDACTED]"
    finally:
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_redact_text_only_masks_values():
    assert redact_text("手机号 13800138000") == "手机号 [REDACTED]"


def test_check_input_truncates_without_rejecting():
    result = check_input("啊" * 3000, max_chars=100)
    assert result.allowed
    assert len(result.text) == 100
    assert any("截断" in reason for reason in result.reasons)


def test_check_output_redacts_and_flags_false_confirmation():
    result = check_output("已为您预约成功，身份证 110101198805123456")
    assert "[REDACTED]" in result.text
    assert any("未调用工具" in reason for reason in result.reasons)
