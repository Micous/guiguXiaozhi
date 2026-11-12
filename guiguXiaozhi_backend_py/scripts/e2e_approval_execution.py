"""审批端到端验收：用户确认后，预约**必须真的发生**。

真机场景（2026-11-12，8081 + 5173）：用户点了「确认执行」，7B 连续两轮
"只说不做"（"请在确认窗口核对…" → "正在为您提交预约"），两轮都零工具调用
→ token 白拿、预约没发生。本脚本用真实 HTTP 把这套流程跑一遍，逐条核对：

    1. 第一轮对话 → 后端下发审批 PENDING（拿到 approval_id）
    2. approve → 拿到一次性 token
    3. 带 token 重发 → 工具**真的执行**（appointment 表出现记录）
    4. 用户看到的最终文本是真实结果（不是"请确认"）
    5. 指标 / trace 上能看出走的是"服务端确定性执行"

    python scripts/e2e_approval_execution.py                 # 直连 8081
    python scripts/e2e_approval_execution.py --base-url http://127.0.0.1:5174/api
    python scripts/e2e_approval_execution.py --keep          # 保留数据库记录

本地 7B 单轮 25–70s，全流程请留足时间（默认单轮 240s 超时）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_appointment import (  # noqa: E402
    DEFAULT_NAME,
    cleanup,
    mysql_rows,
    next_workday,
)

# 校验位合法的测试身份证号，只用于本地联调
DEFAULT_ID_CARD = "610121199401032831"
DEPARTMENT = "呼吸内科"
SLOT = "上午"

APPROVAL_SENTINEL = re.compile(r"\[\[XIAOZHI_APPROVAL\]\](.*?)\[\[/XIAOZHI_APPROVAL\]\]", re.S)


# ---------- 认证 ----------


def auth_token(base_url: str) -> Optional[str]:
    """用 bootstrap 凭证换 JWT（认证关闭时返回 None）。"""
    from xiaozhi.config import get_settings

    settings = get_settings()
    if not settings.auth_enabled:
        return None
    ops_subjects = settings.auth_ops_subjects or []
    if isinstance(ops_subjects, str):
        ops_subjects = ops_subjects.split(",")
    subjects = [s.strip() for s in ops_subjects if str(s).strip()]
    subject = subjects[0] if subjects else "ops"
    resp = httpx.post(
        f"{base_url}/api/v1/auth/token",
        json={"subject_id": subject, "secret": settings.auth_bootstrap_secret},
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise SystemExit(f"换 token 失败：{resp.status_code} {resp.text[:200]}")
    return resp.json()["access_token"]


def new_session(base_url: str, headers: Dict[str, str]) -> str:
    resp = httpx.post(f"{base_url}/api/v1/sessions", headers=headers, timeout=15.0)
    if resp.status_code != 200:
        raise SystemExit(f"创建会话失败：{resp.status_code} {resp.text[:200]}")
    return resp.json()["session_id"]


# ---------- 对话 ----------


def chat(
    base_url: str,
    headers: Dict[str, str],
    memory_id: str,
    message: str,
    approval_token: str = "",
    timeout: float = 240.0,
) -> Tuple[str, str]:
    """发一句对话，返回 (完整文本, trace_id)。"""
    payload: Dict[str, Any] = {"memoryId": memory_id, "message": message}
    if approval_token:
        payload["approvalToken"] = approval_token
    started = time.monotonic()
    text = ""
    trace_id = ""
    with httpx.stream(
        "POST", f"{base_url}/xiaozhi/chat", json=payload, headers=headers, timeout=timeout
    ) as resp:
        trace_id = resp.headers.get("x-trace-id", "")
        if resp.status_code != 200:
            raise SystemExit(f"chat 失败：{resp.status_code} {resp.read()[:200]!r}")
        for chunk in resp.iter_text():
            text += chunk
    elapsed = time.monotonic() - started
    print(f"  ← {elapsed:5.1f}s trace={trace_id} 文本={len(text)} 字")
    return text, trace_id


def find_approval_id(text: str) -> Optional[str]:
    """从裸文本流的结构化标记里取 approval_id。

    标记内容来自 ``ApprovalRequiredEvent.metadata``：``approval_id`` 在
    ``detail`` 里（2026-11-12 踩过：直接取顶层永远是空）。
    """
    match = APPROVAL_SENTINEL.search(text)
    if not match:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    detail = payload.get("detail") or {}
    return detail.get("approval_id") or payload.get("approval_id")


def approve(base_url: str, headers: Dict[str, str], approval_id: str) -> str:
    resp = httpx.post(
        f"{base_url}/api/v1/approvals/{approval_id}/approve", headers=headers, timeout=15.0
    )
    if resp.status_code != 200:
        raise SystemExit(f"确认审批失败：{resp.status_code} {resp.text[:200]}")
    return resp.json()["approval_token"]


def metric_counter(base_url: str, headers: Dict[str, str], name: str) -> float:
    try:
        resp = httpx.get(f"{base_url}/metrics", headers=headers, timeout=15.0)
        counters = (resp.json().get("snapshot") or {}).get("counters") or {}
        return float(counters.get(name, 0))
    except Exception:
        return -1.0


# ---------- 主流程 ----------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8081")
    parser.add_argument("--id-card", default=DEFAULT_ID_CARD)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--department", default=DEPARTMENT)
    parser.add_argument("--date", default="")
    parser.add_argument("--slot", default=SLOT)
    parser.add_argument("--keep", action="store_true", help="保留数据库记录（默认结束后清理）")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    date = args.date or next_workday(3)

    token = auth_token(base)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    session_id = new_session(base, headers) if token else "e2e-approval-session"

    # 先清掉同一身份证+日期的历史记录，避免"已有预约"幂等命中干扰判断
    cleanup(args.id_card)
    auto_before = metric_counter(base, headers, "approval_auto_executions")

    request_text = (
        f"帮我预约{args.department}，{args.name}，{args.id_card}，{date}，{args.slot}。"
    )
    print(f"\n[1/3] 发起预约：{request_text}")
    first_text, first_trace = chat(base, headers, session_id, request_text)
    print(f"  回复：{first_text.strip()[:160]}")

    approval_id = find_approval_id(first_text)
    if not approval_id:
        print("\n❌ 没有拿到 approval_id —— 审批门控没有生效（检查 APPROVAL_ENABLED）")
        return 2
    print(f"  审批单号：{approval_id}")

    print("\n[2/3] 用户点「确认执行」")
    approval_token = approve(base, headers, approval_id)
    print(f"  一次性 token：{approval_token[:12]}…")

    print("\n[3/3] 带 token 重发（模型可能'只说不做'，由服务端确定性执行兜底）")
    second_text, second_trace = chat(base, headers, session_id, request_text, approval_token)
    shown = APPROVAL_SENTINEL.sub("", second_text).strip()
    print(f"  回复：{shown[:220]}")

    # ---- 核对业务事实（不看模型怎么说）----
    rows = mysql_rows(args.id_card, date)
    auto_after = metric_counter(base, headers, "approval_auto_executions")

    print("\n===== 验收结果 =====")
    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"  {'✅' if passed else '❌'} {name}{('：' + detail) if detail else ''}")

    check("appointment 表真的写入了一条预约", len(rows) == 1, f"{len(rows)} 条")
    if rows:
        row = rows[0]
        check("科室正确", row.get("department") == args.department, str(row.get("department")))
        check("日期正确", str(row.get("date")) == date, str(row.get("date")))
    check(
        "用户看到的是结果而不是'请确认'",
        ("预约成功" in second_text) or ("已有预约" in second_text),
        shown[:60],
    )
    if auto_before >= 0:
        check(
            "走的是服务端确定性执行（approval_auto_executions +1）",
            auto_after > auto_before,
            f"{auto_before:.0f} → {auto_after:.0f}",
        )

    print(f"\n  trace：第一轮 {first_trace} / 执行轮 {second_trace}")
    print(f"  详情：{base}/trace/{second_trace}")

    if not args.keep and rows:
        removed = cleanup(args.id_card)
        print(f"  已清理测试记录：{removed} 条")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
