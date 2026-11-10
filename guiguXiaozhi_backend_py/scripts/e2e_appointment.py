"""B1 端到端联调：问诊 → 分导诊 → 挂号 → MySQL 落库 → 取消。

用**同一个 memoryId** 连续多轮调用 `/xiaozhi/chat`，模拟前端 UI 的对话流程；
每一步都从 `X-Trace-Id` 回查追踪树，确认工具真的被调用（而不是模型口头承诺），
并直连 MySQL 核对 `appointment` 表的写入与删除。

    python scripts/e2e_appointment.py                              # 直连 8081
    python scripts/e2e_appointment.py --base-url http://127.0.0.1:5174/api  # 走前端 Vite 代理
    python scripts/e2e_appointment.py --date 2026-11-20 --output logs/e2e-report.json

注意：本地 7B 模型单轮约 25–60s，全流程请留足时间（默认单轮 180s 超时）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 校验位合法的测试身份证号；只用于本地联调，不会写入真实业务数据
DEFAULT_ID_CARD = "110101198805123459"
DEFAULT_NAME = "测试患者"
DEPARTMENT = "神经内科"
SLOT = "上午"


def next_workday(days_ahead: int = 3) -> str:
    """挑一个未来的工作日（Mock 号源周末不出诊）"""
    target = date.today() + timedelta(days=days_ahead)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target.isoformat()


# ---------- MySQL 直连核对 ----------

def _mysql_config() -> Dict[str, Any]:
    from xiaozhi.config import get_settings

    s = get_settings()
    return {
        "host": s.mysql_host,
        "port": s.mysql_port,
        "user": s.mysql_user,
        "password": s.mysql_password,
        "database": s.mysql_database,
        "connection_timeout": 5,
    }


def mysql_rows(id_card: str, appointment_date: Optional[str] = None) -> List[Dict[str, Any]]:
    import mysql.connector

    conn = mysql.connector.connect(**_mysql_config())
    try:
        cur = conn.cursor(dictionary=True)
        sql = (
            "SELECT id, username, id_card, department, `date`, `time`, doctor_name "
            "FROM appointment WHERE id_card=%s"
        )
        params: List[Any] = [id_card]
        if appointment_date:
            sql += " AND `date`=%s"
            params.append(appointment_date)
        cur.execute(sql, tuple(params))
        return list(cur.fetchall())
    finally:
        conn.close()


def cleanup(id_card: str) -> int:
    """清掉本测试身份证号的历史残留，避免上一次失败影响判定"""
    import mysql.connector

    conn = mysql.connector.connect(**_mysql_config())
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM appointment WHERE id_card=%s", (id_card,))
        affected = cur.rowcount
        conn.commit()
        return affected
    finally:
        conn.close()


# ---------- 对话与追踪 ----------

def trace_tools(client: httpx.Client, trace_id: str) -> List[Dict[str, Any]]:
    if not trace_id:
        return []
    try:
        tree = client.get(f"/trace/{trace_id}", timeout=10.0).json()
    except Exception:
        return []
    found: List[Dict[str, Any]] = []

    def walk(nodes: List[Dict[str, Any]]) -> None:
        for node in nodes:
            if node.get("name") == "tool.execute":
                attrs = node.get("attributes") or {}
                found.append(
                    {
                        "tool": attrs.get("tool_name"),
                        "success": attrs.get("success"),
                        "duration_ms": node.get("duration_ms"),
                    }
                )
            walk(node.get("children") or [])

    walk(tree.get("root_spans") or [])
    return found


def render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# 硅谷小智 · 端到端挂号全链路报告（B1）",
        "",
        f"- 目标：`{report['base_url']}` · 预约日期 `{report['appointment_date']}` / {SLOT}",
        f"- 结论：**{'通过 ✅' if report['passed'] else '未通过 ❌'}**",
        f"- 问诊/分导诊：{'✅' if report['consult_ok'] else '❌'} · "
        f"挂号落库：{'✅' if report['book_ok'] else '❌'} · "
        f"取消删除：{'✅' if report['cancel_ok'] else '❌'}",
        "",
        "| 步骤 | 耗时 | 工具调用 | 回答摘要 |",
        "|---|---|---|---|",
    ]
    for step in report["steps"]:
        tools = ", ".join(
            f"{t['tool']}({'成功' if t['success'] else '失败'})" for t in step["tools"]
        ) or "—"
        summary = step["text"][:100].replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {step['label']} | {step['elapsed_ms']} ms | {tools} | {summary} |"
        )
    lines += [
        "",
        "## 数据库核对",
        "",
        f"- 挂号后记录数：`{report['rows_after_book']}`",
        f"- 取消后记录数：`{report['rows_after_cancel']}`",
        "",
        "说明：追踪树里的 `tool.execute` span 是「工具真的被调用」的证据；"
        "仅凭模型回答里的\"已为您预约成功\"不算通过。",
        "",
    ]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    appointment_date = args.date or next_workday()
    memory_id = f"e2e-{int(time.time())}"
    steps: List[Dict[str, Any]] = []

    if args.cleanup:
        try:
            removed = cleanup(args.id_card)
            print(f"[准备] 清理测试身份证历史记录 {removed} 条")
        except Exception as exc:
            print(f"[警告] MySQL 清理失败：{type(exc).__name__}: {exc}", file=sys.stderr)

    client = httpx.Client(
        base_url=args.base_url, timeout=args.timeout, trust_env=False
    )
    try:
        probe = client.get("/health", timeout=10.0)
        if probe.status_code != 200:
            print(f"❌ 目标服务不可用：{args.base_url}/health → {probe.status_code}", file=sys.stderr)
            return 2
        print(f"[准备] 目标 {args.base_url} 已就绪（{probe.json().get('service')}）")

        def chat(label: str, message: str) -> str:
            started = time.perf_counter()
            resp = client.post(
                "/xiaozhi/chat",
                json={"memoryId": memory_id, "message": message},
                timeout=args.timeout,
            )
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            text = resp.text
            trace_id = resp.headers.get("X-Trace-Id", "")
            tools = trace_tools(client, trace_id)
            steps.append(
                {
                    "label": label,
                    "message": message,
                    "status": resp.status_code,
                    "text": text,
                    "elapsed_ms": elapsed,
                    "trace_id": trace_id,
                    "tools": tools,
                }
            )
            print(f"\n--- {label} · {elapsed} ms · trace={trace_id[:8]} ---")
            print(f"工具：{tools or '（无）'}")
            print(f"回答：{text[:300].strip()}")
            return text

        # 1) 问诊 + 分导诊
        consult_text = chat(
            "问诊/分导诊",
            "我最近反复头痛，偶尔还头晕，应该挂什么科？请用一句话回答。",
        )
        consult_ok = bool(consult_text.strip())

        # 2) 挂号：按系统提示词的规则一次性给齐所有必填信息
        book_message = (
            f"帮我预约挂号。患者姓名：{args.name}，身份证号：{args.id_card}，"
            f"预约科室：{DEPARTMENT}，预约日期：{appointment_date}，预约时段：{SLOT}。"
            "信息已齐全，请直接调用预约挂号工具提交预约。"
        )
        chat("提交预约", book_message)
        rows_after_book = mysql_rows(args.id_card, appointment_date)

        # 3) 若模型没有按提示词调用工具，给一次明确的补救指令
        if not rows_after_book:
            print("\n[重试] 未检测到落库，追加一次明确的工具调用指令")
            chat(
                "提交预约（补救）",
                f"请现在立刻调用“预约挂号”工具，参数：姓名 {args.name}、"
                f"身份证号 {args.id_card}、科室 {DEPARTMENT}、日期 {appointment_date}、"
                f"时段 {SLOT}。不要只用文字回复，必须调用工具完成提交。",
            )
            rows_after_book = mysql_rows(args.id_card, appointment_date)

        book_ok = len(rows_after_book) > 0
        print(f"\n[数据库] 挂号后记录：{rows_after_book}")

        # 4) 取消预约
        cancel_text = chat(
            "取消预约",
            f"请帮我取消刚才的预约。姓名：{args.name}，身份证号：{args.id_card}，"
            f"科室：{DEPARTMENT}，日期：{appointment_date}，时段：{SLOT}。"
            "请调用取消预约挂号工具。",
        )
        rows_after_cancel = mysql_rows(args.id_card, appointment_date)

        # 本地 7B 模型偶发"不调工具却口头说已取消"；给一次带事实反馈的补救指令
        if rows_after_cancel and args.retry_cancel:
            print("\n[重试] 取消后数据库仍有记录，追加一次明确的工具调用指令")
            chat(
                "取消预约（补救）",
                "系统记录显示这条预约仍然存在，刚才并没有真正取消。"
                "请立刻调用“取消预约挂号”工具，参数："
                f"姓名 {args.name}、身份证号 {args.id_card}、科室 {DEPARTMENT}、"
                f"日期 {appointment_date}、时段 {SLOT}。"
                "必须调用工具完成删除，不要只用文字回复已取消。",
            )
            rows_after_cancel = mysql_rows(args.id_card, appointment_date)

        cancel_ok = book_ok and len(rows_after_cancel) == 0
        print(f"[数据库] 取消后记录：{rows_after_cancel}")

        report = {
            "base_url": args.base_url,
            "memory_id": memory_id,
            "appointment_date": appointment_date,
            "department": DEPARTMENT,
            "slot": SLOT,
            "steps": steps,
            "consult_ok": consult_ok,
            "book_ok": book_ok,
            "cancel_ok": cancel_ok,
            "rows_after_book": len(rows_after_book),
            "rows_after_cancel": len(rows_after_cancel),
            "passed": consult_ok and book_ok and cancel_ok,
            "cancel_text_preview": cancel_text[:200],
        }
    finally:
        client.close()

    out_json = Path(args.output)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md = out_json.with_suffix(".md")
    out_md.write_text(render_markdown(report), encoding="utf-8")
    print(f"\n报告：{out_json} · {out_md}")
    print("结论：" + ("✅ 全链路通过" if report["passed"] else "❌ 未通过（详见报告）"))
    return 0 if report["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="硅谷小智挂号全链路端到端联调（B1）")
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8081",
        help="后端地址；可传 Vite 代理地址如 http://127.0.0.1:5174/api",
    )
    parser.add_argument("--timeout", type=float, default=180.0, help="单轮对话超时秒数")
    parser.add_argument("--date", help="预约日期 YYYY-MM-DD（默认自动选未来工作日）")
    parser.add_argument("--name", default=DEFAULT_NAME, help="测试患者姓名")
    parser.add_argument("--id-card", default=DEFAULT_ID_CARD, help="测试身份证号（校验位合法）")
    parser.add_argument(
        "--output", default=str(ROOT / "logs" / "e2e-report.json"), help="JSON 报告路径"
    )
    parser.add_argument(
        "--no-cleanup", dest="cleanup", action="store_false",
        help="不清理该测试身份证号的历史记录",
    )
    parser.add_argument(
        "--no-retry-cancel", dest="retry_cancel", action="store_false",
        help="取消未落库时不追加补救轮（用于暴露模型不调工具的问题）",
    )
    parser.set_defaults(cleanup=True, retry_cancel=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
