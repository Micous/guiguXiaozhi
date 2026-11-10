"""Ollama 模型接入（对话 / 流式对话 / 向量化）。

关键点（都来自 Java 版的实测教训）：
  * 请求体必须 UTF-8 编码（Java 版踩过 Spring RestClient 用 ISO-8859-1 把中文变成 "?"）
  * temperature 默认 0.1、num_ctx 8192（小模型工具调用稳定性的前提）
  * 模型偶发返回空响应 → 需要上层重试（这里提供 retry 参数）
  * 走 Ollama 原生 /api/chat（不是 OpenAI 兼容端点），工具 schema 形态与已验证的一致
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

import httpx

from xiaozhi.core.message import ToolCall
from xiaozhi.core.tool import ToolDefinition
from xiaozhi.models.parser import OllamaStreamParser, parse_tool_arguments


@dataclass
class ChatTurn:
    """一次模型调用的结果"""

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    eval_tokens: int = 0
    done_reason: str = ""

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class ChatModel(ABC):
    """对话模型抽象"""

    @abstractmethod
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[Sequence[ToolDefinition]] = None,
    ) -> ChatTurn:
        """非流式对话"""

    @abstractmethod
    def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[Sequence[ToolDefinition]] = None,
    ) -> AsyncIterator[str]:
        """流式对话，逐段产出文本"""


class EmbeddingModel(ABC):
    """向量化模型抽象"""

    @abstractmethod
    async def embed(self, text: str) -> List[float]:
        """单条向量化"""

    @abstractmethod
    async def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        """批量向量化"""

    @abstractmethod
    def dimension(self) -> int:
        """向量维度"""


class OllamaChatModel(ChatModel):
    """Ollama 对话模型"""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:7b",
        temperature: float = 0.1,
        num_ctx: int = 8192,
        timeout: float = 300.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.timeout = timeout
        self._client = client

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _payload(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[Sequence[ToolDefinition]],
        stream: bool,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "options": {
                "temperature": self.temperature,
                "num_ctx": self.num_ctx,
            },
        }
        if tools:
            payload["tools"] = [t.to_tool_payload() for t in tools]
        return payload

    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[Sequence[ToolDefinition]] = None,
    ) -> ChatTurn:
        """非流式对话"""
        payload = self._payload(messages, tools, stream=False)
        resp = await self.client.post(f"{self.base_url}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()

        message = data.get("message") or {}
        tool_calls = [
            ToolCall(
                id=str(call.get("id") or ""),
                name=str((call.get("function") or {}).get("name") or ""),
                arguments=parse_tool_arguments((call.get("function") or {}).get("arguments")),
            )
            for call in message.get("tool_calls") or []
        ]
        return ChatTurn(
            text=message.get("content") or "",
            tool_calls=tool_calls,
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            eval_tokens=int(data.get("eval_count") or 0),
            done_reason=str(data.get("done_reason") or ""),
        )

    async def stream_chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[Sequence[ToolDefinition]] = None,
        result_holder: Optional[Dict[str, Any]] = None,
    ) -> AsyncIterator[str]:
        """流式对话，逐段产出**文本增量**。

        工具调用要等流结束才知道（Ollama 的 tool_calls 在 done 时才完整），
        所以把最终结果放进 ``result_holder["turn"]``，由调用方在流结束后取。
        """
        payload = self._payload(messages, tools, stream=True)
        parser = OllamaStreamParser()
        async with self.client.stream(
            "POST", f"{self.base_url}/api/chat", json=payload
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                delta = parser.feed(line)
                if delta:
                    yield delta

        parsed = parser.finish()
        if result_holder is not None:
            result_holder["turn"] = ChatTurn(
                text=parsed.text,
                tool_calls=[
                    ToolCall(
                        id=tc.id or tc.name,
                        name=tc.name,
                        arguments=tc.parse_arguments(),
                    )
                    for tc in parsed.tool_calls
                ],
                prompt_tokens=parsed.prompt_tokens,
                eval_tokens=parsed.eval_tokens,
                done_reason=parsed.done_reason,
            )


class OllamaEmbeddingModel(EmbeddingModel):
    """Ollama 向量化模型"""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "andersc/qwen3-embedding:0.6b",
        timeout: float = 120.0,
        client: Optional[httpx.AsyncClient] = None,
        _dimension: Optional[int] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._client = client
        self._dimension = _dimension

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def embed(self, text: str) -> List[float]:
        vectors = await self.embed_batch([text])
        return vectors[0] if vectors else []

    async def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        """批量向量化（一次请求，减少往返）"""
        if not texts:
            return []
        resp = await self.client.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model, "input": list(texts)},
        )
        resp.raise_for_status()
        vectors = resp.json().get("embeddings") or []
        if vectors and self._dimension is None:
            self._dimension = len(vectors[0])
        return vectors

    def dimension(self) -> int:
        """已知维度；未知时抛错（应先调用 ensure_dimension）"""
        if self._dimension is None:
            raise RuntimeError("维度未知：需先调用 ensure_dimension() 或 embed_batch()")
        return self._dimension

    async def ensure_dimension(self) -> int:
        """确保维度已知（未知则用一次探针请求确定）"""
        if self._dimension is None:
            vectors = await self.embed_batch(["dimension probe"])
            if vectors:
                self._dimension = len(vectors[0])
        return self._dimension or 0
