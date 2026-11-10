"""追踪存储：内存环形缓冲 + JSONL 落盘 + 追踪树构建。

这是"想看完整跟踪流程"的落地层：
  - 每次对话的所有 span 汇总成一条 trace，按 trace_id 可查
  - 追踪树把 span 按 parent_span_id 组装成层级，页面直接渲染
  - 落盘后重启仍可回看历史对话
"""

from __future__ import annotations

import json
import os
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from xiaozhi.reliability.tracing import Span, register_span_sink


class TraceStore:
    """追踪数据仓库（线程安全）"""

    def __init__(self, max_traces: int = 500, persist_dir: Optional[str] = None) -> None:
        self.max_traces = max_traces
        self.persist_dir = persist_dir
        self._lock = threading.Lock()
        # trace_id -> {span_id: span_dict}
        self._traces: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._summary: Dict[str, Dict[str, Any]] = {}
        if persist_dir:
            os.makedirs(persist_dir, exist_ok=True)
        register_span_sink(self._on_span)

    # ---------- 收集 ----------

    def _on_span(self, span: Span) -> None:
        """span 结束时的回调"""
        record = span.to_dict()
        with self._lock:
            trace = self._traces.setdefault(record["trace_id"], {"spans": {}, "order": []})
            trace["spans"][record["span_id"]] = record
            trace["order"].append(record["span_id"])
            self._traces.move_to_end(record["trace_id"])

            # 维护 trace 摘要
            summary = self._summary.setdefault(
                record["trace_id"],
                {
                    "trace_id": record["trace_id"],
                    "started_at": record["start_time"],
                    "span_count": 0,
                    "total_duration_ms": 0.0,
                    "has_error": False,
                    "degraded": False,
                    "tools": [],
                    "model": None,
                    "input_preview": None,
                },
            )
            summary["span_count"] = len(trace["spans"])
            summary["total_duration_ms"] = round(
                sum(s["duration_ms"] for s in trace["spans"].values()), 2
            )
            if record.get("error"):
                summary["has_error"] = True
            attrs = record.get("attributes") or {}
            if record["name"] == "tool.execute" and attrs.get("tool_name"):
                summary["tools"].append(attrs["tool_name"])
            if record["name"] == "llm.chat" and attrs.get("model"):
                summary["model"] = attrs["model"]
            if record["name"] == "http.request" and attrs.get("message_preview"):
                summary["input_preview"] = attrs["message_preview"]

            # 淘汰最旧的 trace
            while len(self._traces) > self.max_traces:
                oldest, _ = self._traces.popitem(last=False)
                self._summary.pop(oldest, None)

        self._persist(record["trace_id"])

    # ---------- 查询 ----------

    def list_traces(self, limit: int = 50) -> List[Dict[str, Any]]:
        """列出最近的 trace 摘要（新的在前）"""
        with self._lock:
            items = list(self._summary.values())
        items.sort(key=lambda x: x.get("started_at") or "", reverse=True)
        return items[:limit]

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """取某条 trace 的原始 span 列表"""
        with self._lock:
            trace = self._traces.get(trace_id)
            if trace is None:
                return None
            return {
                "trace_id": trace_id,
                "spans": [trace["spans"][sid] for sid in trace["order"]],
                "summary": self._summary.get(trace_id, {}),
            }

    def get_trace_tree(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """取某条 trace 的层级树"""
        raw = self.get_trace(trace_id)
        if raw is None:
            return None

        spans = raw["spans"]
        by_id = {s["span_id"]: s for s in spans}
        children_map: Dict[Optional[str], List[Dict[str, Any]]] = {}
        for span in spans:
            children_map.setdefault(span.get("parent_span_id"), []).append(span)

        def build(parent_id: Optional[str]) -> List[Dict[str, Any]]:
            nodes = []
            for child in children_map.get(parent_id, []):
                node = dict(child)
                node["children"] = build(child["span_id"])
                nodes.append(node)
            return nodes

        return {
            "trace_id": trace_id,
            "summary": raw["summary"],
            "root_spans": build(None),
        }

    def stats(self) -> Dict[str, Any]:
        """全局统计（供监控面板用）"""
        with self._lock:
            traces = list(self._summary.values())
        if not traces:
            return {"trace_count": 0, "error_rate": 0.0, "avg_duration_ms": 0.0}
        error_count = sum(1 for t in traces if t.get("has_error"))
        return {
            "trace_count": len(traces),
            "error_rate": round(error_count / len(traces), 4),
            "avg_duration_ms": round(
                sum(t.get("total_duration_ms", 0.0) for t in traces) / len(traces), 2
            ),
            "degraded_count": sum(1 for t in traces if t.get("degraded")),
        }

    # ---------- 落盘 ----------

    def _persist(self, trace_id: str) -> None:
        """把 trace 追加写入 JSONL"""
        if not self.persist_dir:
            return
        try:
            raw = self.get_trace(trace_id)
            if not raw:
                return
            path = os.path.join(self.persist_dir, "traces.jsonl")
            with self._lock:
                with open(path, "a", encoding="utf-8") as fp:
                    fp.write(json.dumps(raw, ensure_ascii=False, default=str) + "\n")
        except Exception:  # 落盘失败不影响主流程
            pass

    def load_from_disk(self) -> int:
        """启动时把历史 trace 读回内存"""
        if not self.persist_dir:
            return 0
        path = os.path.join(self.persist_dir, "traces.jsonl")
        if not os.path.exists(path):
            return 0
        loaded = 0
        try:
            with open(path, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    raw = json.loads(line)
                    trace_id = raw.get("trace_id")
                    if not trace_id:
                        continue
                    with self._lock:
                        trace = self._traces.setdefault(trace_id, {"spans": {}, "order": []})
                        for span in raw.get("spans", []):
                            trace["spans"][span["span_id"]] = span
                            trace["order"].append(span["span_id"])
                        self._traces.move_to_end(trace_id)
                        self._summary[trace_id] = raw.get("summary", {"trace_id": trace_id})
                    loaded += 1
        except Exception:
            return loaded
        return loaded


# 全局默认实例（应用启动时装配，见 application.py）
_default_store: Optional[TraceStore] = None


def set_default_store(store: TraceStore) -> None:
    global _default_store
    _default_store = store


def get_default_store() -> Optional[TraceStore]:
    return _default_store
