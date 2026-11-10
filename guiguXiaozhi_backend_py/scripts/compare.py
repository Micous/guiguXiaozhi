"""Java(8080 / LangChain4j) vs Python(8081 / 自研 Harness) A/B 对比。

对应 PLAN.md「3.3.4 与框架对比」与「七、验收标准」。
同一批问题双发两套后端，产出：最终回答、耗时、Python 侧的 span 数与工具调用。
Python 侧通过响应头 ``X-Trace-Id`` 精确关联到 ``/trace/{id}``。

用法：
    python scripts/compare.py                              # 默认问题集，打印 Markdown
    python scripts/compare.py --question "头痛挂什么科" --question "抽血要空腹吗"
    python scripts/compare.py --output report.md --json report.json
    python scripts/compare.py --java http://host:8080 --python http://host:8081
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_QUESTIONS = [
    "最近总是头痛，应该挂什么科？",
    "抽血检查需要空腹吗？",
    "医院地址在哪里？",
    "神经内科有哪些专家？",
]

JAVA_BASE = "http://localhost:8080"
PYTHON_BASE = "http://localhost:8081"


async def call_chat(
    client: httpx.AsyncClient,
    base_url: str,
    question: str,
    memory_id: str,
    timeout: float,
    label: str,
) -> Dict[str, Any]:
    """调用一端的对话接口，收集裸文本流"""
    url = f"{base_url.rstrip('/')}/xiaozhi/chat"
    started = time.perf_counter()
    chunks: List[str] = []
    try:
        async with client.stream(
            "POST",
            url,
            json={"memoryId": memory_id, "message": question},
            timeout=timeout,
        ) as resp:
            trace_id = resp.headers.get("X-Trace-Id", "")
            status = resp.status_code
            async for chunk in resp.aiter_text():
                chunks.append(chunk)
        return {
            "label": label,
            "ok": status == 200,
            "status": status,
            "text": "".join(chunks),
            "chars": len("".join(chunks)),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "trace_id": trace_id,
        }
    except httpx.HTTPError as exc:
        return {
            "label": label,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "text": "",
            "chars": 0,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "trace_id": "",
        }


async def fetch_trace(
    client: httpx.AsyncClient, base_url: str, trace_id: str
) -> Optional[Dict[str, Any]]:
    if not trace_id:
        return None
    try:
        resp = await client.get(
            f"{base_url.rstrip('/')}/trace/{trace_id}", timeout=5.0
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except httpx.HTTPError:
        return None


def summarize_trace(tree: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从追踪树里统计 span 数与工具调用"""
    if not tree:
        return {"available": False, "span_count": 0, "tools": []}

    spans: List[Dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "span_id" in node and "name" in node:
                spans.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(tree.get("root_spans", []))
    tools = [
        span.get("attributes", {}).get("tool_name")
        for span in spans
        if span.get("name") == "tool.execute"
    ]
    return {
        "available": True,
        "span_count": len(spans),
        "tools": [t for t in tools if t],
        "total_duration_ms": tree.get("summary", {}).get("total_duration_ms"),
    }


async def compare_one(
    client: httpx.AsyncClient,
    index: int,
    question: str,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    # Java 版 ChatForm.memoryId 是 Long，必须传数字；Python 版会自行 str() 转换
    memory_id = int(time.time()) + index
    java, py = await asyncio.gather(
        call_chat(client, args.java, question, memory_id, args.timeout, "java"),
        call_chat(client, args.python, question, memory_id, args.timeout, "python"),
    )
    trace = await fetch_trace(client, args.python, py.get("trace_id", ""))
    return {
        "question": question,
        "java": java,
        "python": py,
        "python_trace": summarize_trace(trace),
    }


def _cell(text: str, limit: int = 160) -> str:
    cleaned = (text or "").replace("|", "\\|").replace("\n", " ").strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "…"
    return cleaned or "(空)"


def render_markdown(results: List[Dict[str, Any]], args: argparse.Namespace) -> str:
    lines = [
        "# 硅谷小智 · Java vs Python 对比报告",
        "",
        f"- Java：`{args.java}` · Python：`{args.python}`",
        f"- 问题数：{len(results)} · 单请求超时：{args.timeout}s",
        "- 过程可见性：Python 侧通过 `X-Trace-Id` 关联 `/trace/{id}`，Java 侧无追踪接口",
        "",
        "| # | 问题 | Java 回答 | Python 回答 | Java 耗时 | Python 耗时 | Python span 数 | Python 工具调用 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    def failure(item: Dict[str, Any]) -> str:
        return f"❌ {_cell(item.get('error') or 'HTTP ' + str(item.get('status', '连接失败')))}"

    for index, item in enumerate(results, 1):
        java, py = item["java"], item["python"]
        trace = item["python_trace"]
        java_text = _cell(java.get("text")) if java.get("ok") else failure(java)
        py_text = _cell(py.get("text")) if py.get("ok") else failure(py)
        lines.append(
            f"| {index} | {_cell(item['question'], 60)} | {java_text} | {py_text} "
            f"| {java.get('elapsed_ms', 0)} ms | {py.get('elapsed_ms', 0)} ms "
            f"| {trace['span_count']} | {', '.join(trace['tools']) or '—'} |"
        )

    lines += [
        "",
        "## 说明",
        "",
        "- 两端使用**相同 memoryId**，但各自独立存储记忆；首次提问无历史干扰。",
        "- Java 版没有 trace 接口，过程可见性只能与 Python 侧的 span 数对照。",
        "- 若某端显示 ❌，说明服务未启动或连接失败；先执行 `./start-local.sh` 起中间件，"
        "再分别启动两套后端。",
        "",
    ]
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    questions: List[str] = list(args.question or []) or list(DEFAULT_QUESTIONS)
    if args.questions_file:
        questions = [
            line.strip()
            for line in Path(args.questions_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]

    async with httpx.AsyncClient() as client:
        results = [
            await compare_one(client, index, question, args)
            for index, question in enumerate(questions, 1)
        ]

    report = render_markdown(results, args)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Markdown 报告已写入 {args.output}")
    else:
        print(report)

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"config": vars(args), "results": results},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"JSON 结果已写入 {args.json}")

    failed = sum(
        1 for item in results if not item["java"]["ok"] or not item["python"]["ok"]
    )
    if failed:
        print(
            f"\n提示：{failed}/{len(results)} 组对比有一端不可用，"
            "请确认 Java(8080) 与 Python(8081) 都已启动。",
            file=sys.stderr,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Java vs Python 后端 A/B 对比")
    parser.add_argument("--java", default=JAVA_BASE, help="Java 版地址（默认 8080）")
    parser.add_argument("--python", default=PYTHON_BASE, help="Python 版地址（默认 8081）")
    parser.add_argument("--question", action="append", help="自定义问题，可重复传入")
    parser.add_argument("--questions-file", help="问题清单文件（每行一个，# 开头为注释）")
    parser.add_argument("--timeout", type=float, default=120.0, help="单请求超时秒数")
    parser.add_argument("--output", help="Markdown 报告输出路径（不填则打印到 stdout）")
    parser.add_argument("--json", help="结构化 JSON 结果输出路径")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
