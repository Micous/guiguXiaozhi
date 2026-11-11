"""Prometheus 文本格式导出（**不引入 prometheus_client**）。

为什么自己渲染：``prometheus_client`` 会为每个指标在进程内维护独立的注册表，
与现有 ``MetricsCollector``（带窗口直方图、可单测断言、可重置）形成两套真源，
一旦两边计数不一致就没法判断哪个是事实。这里只做**渲染**：真源仍是
``MetricsCollector``，导出是纯函数，便于离线单测。

三条纪律：

1. **标签必须闭集**。只允许 ``tool``（工具注册表里的固定几个）与
   ``outcome``（success/failed）。用户 ID / session_id / trace_id / 科室名 /
   错误消息一律**不进 label** —— 高基数标签会让 Prometheus 侧内存与时序库
   直接爆掉，这是比"指标不够细"严重得多的故障。
2. **单位**。内部直方图是毫秒（``request_duration_ms``），导出统一转秒并加
   ``_seconds`` 后缀（Prometheus 惯例），桶边界也按秒定义。
3. **计数器命名**。内部 ``tool.<tool>.<outcome>`` 这类动态计数器拆成
   ``xiaozhi_tool_calls_total{tool=...,outcome=...}``，而不是把工具名编进指标名
   （否则每加一个工具就多一个指标族，告警规则没法写）。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# 直方图桶边界（秒）。覆盖本地 7B 模型 40~300s 的长尾，也覆盖工具调用的毫秒级。
BUCKET_BOUNDS_SECONDS: Tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)

# 关键指标的 HELP 文案（其余用统一兜底文案，不猜语义）
_HELP: Dict[str, str] = {
    "requests_total": "对话请求总数",
    "errors_total": "对话请求失败数",
    "tool_calls_total": "工具调用次数（tool/outcome 为闭集标签）",
    "tool_calls_success": "工具调用成功次数",
    "tool_calls_failed": "工具调用失败次数",
    "llm_calls_total": "模型调用次数",
    "rate_limited_total": "被限流拒绝的请求数",
    "storage_unavailable_total": "会话存储不可用导致的拒服务次数",
    "degraded_total": "降级（RAG/存储不可用）次数",
    "empty_response_retries": "空响应重试次数",
    "hallucination_suspected": "疑似幻觉（谎报完成态）命中次数",
    "hallucination_fallbacks": "幻觉兜底改写次数",
    "false_confirmation_truncated": "虚报成功被截断次数",
    "narrated_tool_call_retries": "口头承诺未调工具触发的纠正重试次数",
    "textual_tool_call_truncated": "文本化工具调用被截断次数",
    "audit_write_failed_total": "审计写盘失败次数（fail-closed 会拒绝执行）",
}

_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9_]")


def _escape_label_value(value: str) -> str:
    """按 Prometheus 文本格式转义标签值：反斜杠、双引号、换行。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _sanitize(name: str) -> str:
    cleaned = _SANITIZE_RE.sub("_", name).strip("_")
    if not cleaned:
        return "unnamed"
    if cleaned[0].isdigit():
        return f"m_{cleaned}"
    return cleaned


def _format_labels(labels: Dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(
        f'{key}="{_escape_label_value(value)}"' for key, value in sorted(labels.items())
    )
    return "{" + inner + "}"


def _format_value(value: float) -> str:
    if isinstance(value, bool):  # bool 是 int 的子类，先拦掉
        return "1" if value else "0"
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(round(float(value), 6))


def _counter_target(name: str) -> Tuple[str, Dict[str, str]]:
    """把内部计数器名映射成 (指标基础名, 闭集标签)。

    ``tool.BookAppointment.success`` → (``tool_calls``, {tool, outcome})
    其它名字原样返回、无标签。
    """
    if name.startswith("tool."):
        parts = name.split(".")
        if len(parts) == 3 and parts[1] and parts[2]:
            return "tool_calls", {"tool": parts[1], "outcome": parts[2]}
    return name, {}


def _counter_metric_name(base: str, namespace: str) -> str:
    metric = f"{namespace}_{_sanitize(base)}"
    return metric if metric.endswith("_total") else f"{metric}_total"


def _histogram_metric_name(name: str, namespace: str) -> str:
    base = f"{namespace}_{_sanitize(name)}"
    # 内部单位是毫秒，导出统一为秒
    for suffix in ("_ms", "_milliseconds"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}_seconds"


def _cumulative_buckets(samples_ms: Sequence[float]) -> List[Tuple[float, int]]:
    """把原始样本（毫秒）折算成累计桶计数（秒）。"""
    total = 0
    result: List[Tuple[float, int]] = []
    upper_bounds_in_ms = [bound * 1000.0 for bound in BUCKET_BOUNDS_SECONDS]
    counts = [0] * len(upper_bounds_in_ms)
    for sample in samples_ms:
        for index, bound_ms in enumerate(upper_bounds_in_ms):
            if sample <= bound_ms:
                counts[index] += 1
                break
    for bound, count in zip(BUCKET_BOUNDS_SECONDS, counts):
        total += count
        result.append((bound, total))
    return result


def render_prometheus(
    snapshot: Dict[str, Any],
    samples: Optional[Dict[str, List[float]]] = None,
    namespace: str = "xiaozhi",
) -> str:
    """把 ``MetricsCollector.snapshot()`` + 原始样本渲染成 Prometheus 文本格式。

    ``samples`` 是 ``{直方图名: [毫秒样本...]}``；缺省时只导出 ``_count``/``_sum``
    （没有样本无法算桶），保证导出永不为空。
    """
    samples = samples or {}
    lines: List[str] = []

    # ---- 计数器（合并同名不同标签的样本族）----
    counters: Dict[str, List[Tuple[Dict[str, str], float]]] = {}
    helps: Dict[str, str] = {}
    for name, value in (snapshot.get("counters") or {}).items():
        base, labels = _counter_target(name)
        metric = _counter_metric_name(base, namespace)
        counters.setdefault(metric, []).append((labels, float(value)))
        helps.setdefault(metric, _HELP.get(base, f"{base}（由 MetricsCollector 导出）"))

    for metric in sorted(counters):
        lines.append(f"# HELP {metric} {_escape_help(helps[metric])}")
        lines.append(f"# TYPE {metric} counter")
        for labels, value in sorted(counters[metric], key=lambda item: sorted(item[0].items())):
            lines.append(f"{metric}{_format_labels(labels)} {_format_value(value)}")

    # ---- 仪表 ----
    gauges = snapshot.get("gauges") or {}
    if gauges:
        for name in sorted(gauges):
            metric = f"{namespace}_{_sanitize(name)}"
            lines.append(
                f"# HELP {metric} {_escape_help(_HELP.get(name, f'{name}（瞬时值）'))}"
            )
            lines.append(f"# TYPE {metric} gauge")
            lines.append(f"{metric} {_format_value(float(gauges[name]))}")

    # ---- 直方图 ----
    histograms = snapshot.get("histograms") or {}
    for name in sorted(histograms):
        summary = histograms[name] or {}
        count = int(summary.get("count") or 0)
        metric = _histogram_metric_name(name, namespace)
        values = samples.get(name) or []
        # 用原始样本算 _sum（毫秒 → 秒）；没有样本时退回 avg*count
        if values:
            total_seconds = sum(values) / 1000.0
        else:
            total_seconds = (float(summary.get("avg_ms") or 0.0) * count) / 1000.0

        lines.append(
            f"# HELP {metric} {_escape_help(_HELP.get(name, f'{name}（毫秒样本折算为秒）'))}"
        )
        lines.append(f"# TYPE {metric} histogram")
        if values:
            for bound, cumulative in _cumulative_buckets(values):
                lines.append(f'{metric}_bucket{{le="{_format_value(bound)}"}} {cumulative}')
        lines.append(f'{metric}_bucket{{le="+Inf"}} {count}')
        lines.append(f"{metric}_sum {_format_value(total_seconds)}")
        lines.append(f"{metric}_count {count}")

    # ---- 进程信息（便于多实例区分；不带高基数标签）----
    lines.append(f"# HELP {namespace}_metrics_snapshot_loaded 1 表示导出成功")
    lines.append(f"# TYPE {namespace}_metrics_snapshot_loaded gauge")
    lines.append(f"{namespace}_metrics_snapshot_loaded 1")

    return "\n".join(lines) + "\n"


def iter_metric_names(snapshot: Dict[str, Any], namespace: str = "xiaozhi") -> Iterable[str]:
    """导出会产生的指标名（供测试/文档核对，不参与运行时逻辑）。"""
    names = set()
    for name in (snapshot.get("counters") or {}):
        base, _labels = _counter_target(name)
        names.add(_counter_metric_name(base, namespace))
    for name in (snapshot.get("gauges") or {}):
        names.add(f"{namespace}_{_sanitize(name)}")
    for name in (snapshot.get("histograms") or {}):
        names.add(_histogram_metric_name(name, namespace))
    return sorted(names)
