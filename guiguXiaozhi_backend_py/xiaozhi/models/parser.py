"""流式响应解析 —— 把 Ollama 的 NDJSON 增量转成结构化内容块。

对应参考项目 mini_harness/models/parser.py 的 TextBlock / ToolUseBlock 思路，
但这里要处理 Ollama 的两个特殊点：
  1. 流式下 tool_calls 是**分片**到达的（name 先到、arguments 再逐段追加）
  2. arguments 可能是 JSON 字符串，也可能是已解析的对象（两种都要兼容）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TextChunk:
    """文本增量"""

    text: str = ""
    type: str = "text"


@dataclass
class ToolCallChunk:
    """工具调用（分片累积）"""

    id: str = ""
    name: str = ""
    arguments: str = ""  # 原始 JSON 文本
    type: str = "tool_call"

    def parse_arguments(self) -> Dict[str, Any]:
        """把 arguments 文本解析成 dict"""
        if not self.arguments:
            return {}
        try:
            parsed = json.loads(self.arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


@dataclass
class StreamParseResult:
    """一次流式解析的结果"""

    text: str = ""
    tool_calls: List[ToolCallChunk] = field(default_factory=list)
    done_reason: str = ""
    prompt_tokens: int = 0
    eval_tokens: int = 0

    @property
    def has_tool_calls(self) -> bool:
        return any(tc.name for tc in self.tool_calls)


class OllamaStreamParser:
    """Ollama /api/chat 流式解析器"""

    def __init__(self) -> None:
        self.result = StreamParseResult()
        # tool_call 索引 -> 累积器
        self._tool_accum: Dict[int, ToolCallChunk] = {}

    def feed(self, line: str) -> Optional[str]:
        """喂入一行 NDJSON，返回本行的文本增量（无文本则返回 None）"""
        line = (line or "").strip()
        if not line:
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None

        message = payload.get("message") or {}
        delta = message.get("content") or ""
        if delta:
            self.result.text += delta

        for call in message.get("tool_calls") or []:
            index = int(call.get("index", 0))
            acc = self._tool_accum.setdefault(index, ToolCallChunk())
            fn = call.get("function") or {}
            if call.get("id"):
                acc.id = call["id"]
            if fn.get("name"):
                acc.name = fn["name"]
            args = fn.get("arguments")
            if isinstance(args, str):
                acc.arguments += args
            elif isinstance(args, dict):
                # 已经是对象：直接序列化保存（Ollama 有时这样返回）
                acc.arguments = json.dumps(args, ensure_ascii=False)

        if payload.get("done"):
            self.result.done_reason = payload.get("done_reason", "")
            self.result.prompt_tokens = int(payload.get("prompt_eval_count") or 0)
            self.result.eval_tokens = int(payload.get("eval_count") or 0)

        return delta if delta else None

    def finish(self) -> StreamParseResult:
        """结束并返回累积结果"""
        self.result.tool_calls = [
            tc for _, tc in sorted(self._tool_accum.items()) if tc.name
        ]
        return self.result


def parse_tool_arguments(raw: Any) -> Dict[str, Any]:
    """兼容字符串 / 对象两种形态的工具参数"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def estimate_tokens_approx(text: str) -> int:
    """粗略估算 token 数（与分块器同一套口径）

    中文 ≈ 1 字 ≈ 0.6 token，英文/数字 ≈ 4 字符 ≈ 1 token。
    """
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    cjk_chars = len(text) - ascii_chars
    return int(ascii_chars / 4 + cjk_chars / 1.7) + 1
