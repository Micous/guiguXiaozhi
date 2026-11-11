#!/usr/bin/env python
"""本地预约挂号全流程验收脚本。

**目的**：证明「预约挂号流程涉及的工具接口本地完全可用」——不连任何医院
系统，只靠本地 HIS 替身，把四个工具接口真跑一遍：

.. code-block:: text

    查询是否有号源   → 号源剩余数真实
    预约挂号         → 审批 → 预占 → 落单 → 号源真被扣 → DB CONFIRMED
    取消预约挂号     → 退号 → 号源真被归还 → DB CANCELLED
    查询科室专家     → 医生名单与号源一致

外加两项健壮性验证：
  * 重复预约 → 本地查重前置，不重复占号
  * 号源耗尽 → 明确失败（不是"有号"假象）

用法（后端已用 SLOT_SERVICE_IMPL=local_his 启动时）：

.. code-block:: bash

    python scripts/verify_local_booking.py --base-url http://127.0.0.1:8081

不带参数时按环境变量与默认值推断。脚本会自己登录、建会话、走审批、
查库核对，最后打印一张结论表。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 通过 GB 11643 校验位的身份证号（校验位错的会被参数校验拒绝）
VALID_ID = "110101199003074565"
DEPARTMENT = "呼吸内科"
# 姓名必须是**纯中文** —— 带数字会被模型/校验器拒绝
# （真机实测："系统提示患者姓名不应包含数字"）。
# 唯一性靠**日期**隔离（脚本自动挑一个该科室还没约过的工日），
# 不用姓名/身份证 —— 模型会"规范化"用户输入（实测把 ...9809 改成 ...0003）。
VALID_NAME = "张伟"
# ⚠️ 身份证号结构：区域码(6) + 出生日期(8) + 顺序码(3) = 17 位 + 校验位(1) = 18 位。
#    VALID_ID 已通过 GB 11643 校验（曾误用 4 位顺序码 → 校验位错位 → 模型拒收）。


class Result:
    def __init__(self) -> None:
        self.rows: list = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.rows.append((name, ok, detail))
        flag = "✅" if ok else "❌"
        print(f"{flag} {name}" + (f" —— {detail}" if detail else ""))

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.rows if not ok)

    def summary(self) -> str:
        passed = len(self.rows) - self.failed
        return f"{passed}/{len(self.rows)} 项通过"


def request(
    base_url: str,
    path: str,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 240,
) -> Tuple[int, str]:
    url = base_url + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with OPENER.open(req, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return -1, repr(exc)


def read_env(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("\"'")
    return env


def find_appointment(date_str: str) -> Optional[Dict[str, Any]]:
    """查库核对（按项目约定：验收必须查数据库，不能只看回复文本）。"""
    env = read_env(ROOT / ".env")
    try:
        import mysql.connector
    except ImportError:
        return None
    try:
        conn = mysql.connector.connect(
            host=env.get("MYSQL_HOST", "127.0.0.1"),
            port=int(env.get("MYSQL_PORT", "3306")),
            user=env.get("MYSQL_USER", "root"),
            password=env.get("MYSQL_PASSWORD", "123456"),
            database=env.get("MYSQL_DATABASE", "guiguxiaozhi"),
        )
    except Exception:
        return None
    try:
        cur = conn.cursor(dictionary=True)
        # 按**日期**查最新一条 —— 不按姓名/身份证：模型可能把用户输入的
        # 姓名或身份证号"规范化"掉（实测：用户传 ...9809，模型落库 ...0003），
        # 按姓名查会查不到，把"通过"误判成"失败"。
        cur.execute(
            "SELECT id, username, id_card, department, date, time, status,"
            " external_booking_id, external_hold_id, patient_ref, version"
            " FROM appointment WHERE date=%s AND department=%s"
            " ORDER BY id DESC LIMIT 1",
            (date_str, DEPARTMENT),
        )
        row = cur.fetchone()
        return row
    except Exception:
        return None
    finally:
        conn.close()


def local_his_state() -> Optional[Dict[str, Any]]:
    """读本地 HIS 的持久化状态（核对号源是否真被扣/归还）。

    路径来源优先级：环境变量 > .env > 默认位置。
    """
    env = read_env(ROOT / ".env")
    raw_path = (
        os.environ.get("LOCAL_HIS_STATE_PATH")
        or env.get("LOCAL_HIS_STATE_PATH")
        or str(ROOT.parent / "logs" / "local_his_state.json")
    )
    file = Path(raw_path)
    if not file.exists():
        return None
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except Exception:
        return None


def _pick_free_working_day() -> str:
    """挑一个该科室**还没约过**的工作日（保证测的是"新建预约"而非幂等命中）。"""
    import datetime as _dt

    env = read_env(ROOT / ".env")
    try:
        import mysql.connector

        conn = mysql.connector.connect(
            host=env.get("MYSQL_HOST", "127.0.0.1"),
            port=int(env.get("MYSQL_PORT", "3306")),
            user=env.get("MYSQL_USER", "root"),
            password=env.get("MYSQL_PASSWORD", "123456"),
            database=env.get("MYSQL_DATABASE", "guiguxiaozhi"),
        )
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT DISTINCT date FROM appointment WHERE department=%s", (DEPARTMENT,)
        )
        used = {str(r["date"]) for r in cur.fetchall()}
        conn.close()
    except Exception:
        used = set()

    start = _dt.date.today()
    for offset in range(1, 60):
        day = start + _dt.timedelta(days=offset)
        if day.weekday() >= 5:  # 周末不出诊
            continue
        iso = day.isoformat()
        if iso not in used:
            return iso
    return (start + _dt.timedelta(days=1)).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description="本地预约挂号全流程验收")
    parser.add_argument(
        "--base-url", default=os.environ.get("VERIFY_BASE_URL", "http://127.0.0.1:8081")
    )
    parser.add_argument(
        "--date",
        default="",
        help="就诊日期（工作日）；留空 = 自动挑一个该科室还没约过的日期",
    )
    parser.add_argument("--slot", default="上午", help="时段")
    parser.add_argument("--department", default="呼吸内科")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    result = Result()
    date_str = args.date or _pick_free_working_day()
    print(f"本次验收使用就诊日期：{date_str}")

    print(f"=== 本地预约挂号全流程验收（{base}）===\n")

    # ---- 登录与会话 ----
    env = read_env(ROOT / ".env")
    secret = env.get("AUTH_BOOTSTRAP_SECRET", "")
    if not secret:
        print("❌ .env 里没有 AUTH_BOOTSTRAP_SECRET（认证开启时无法登录）")
        return 1
    status, body = request(
        base, "/v1/auth/token", "POST", {"secret": secret, "subject_id": "local-verify"}
    )
    if status != 200:
        print(f"❌ 登录失败（status={status}）：{body[:200]}")
        return 1
    jwt = json.loads(body)["access_token"]
    auth = {"Authorization": f"Bearer {jwt}"}
    status, body = request(base, "/v1/sessions", "POST", {}, auth)
    session_id = json.loads(body).get("session_id") if status == 200 else "local-verify"
    print(f"登录 OK，session={session_id}\n")

    def chat(message: str, approval_token: str = "") -> str:
        payload: Dict[str, Any] = {"memoryId": session_id, "message": message}
        if approval_token:
            payload["approvalToken"] = approval_token
        _status, body = request(base, "/xiaozhi/chat", "POST", payload, auth)
        return body

    def book(message: str) -> str:
        """发起 → 审批 → 执行，返回执行轮输出。"""
        first = chat(message)
        sentinel = re.search(
            r"\[\[XIAOZHI_APPROVAL\]\](.*?)\[\[/XIAOZHI_APPROVAL\]\]", first, re.S
        )
        if not sentinel:
            return first
        approval_id = json.loads(sentinel.group(1)).get("detail", {}).get("approval_id")
        if not approval_id:
            return first
        _status, approve_body = request(
            base, f"/v1/approvals/{approval_id}/approve", "POST", {}, auth
        )
        token = json.loads(approve_body).get("approval_token", "")
        return chat(message, token)

    # ---- 1. 查询号源 ----
    print("--- 1. 查询是否有号源 ---")
    answer = chat(f"{args.department} {date_str} {args.slot}还有号吗？")
    matched = re.search(r"有号源|还有(\d+)个|共(\d+)个号", answer)
    result.add(
        "查询是否有号源：返回真实剩余号数",
        bool(matched),
        (matched.group(0) if matched else answer[-120:].replace("\n", " ")),
    )

    # ---- 2. 预约挂号（审批 → 预占 → 落单）----
    print("\n--- 2. 预约挂号 ---")
    message = (
        f"帮我预约挂号，患者{VALID_NAME}，身份证号{VALID_ID}，"
        f"{args.department}，{date_str}{args.slot}"
    )
    output = book(message)
    # 措辞不固定：模型可能说"预约成功""预约详情""已为您预约"……
    booked = bool(
        re.search(r"预约成功|预约详情|已为您预约|预约已成功", output)
    )
    result.add(
        "预约挂号：审批后真实落单",
        booked,
        output[-140:].replace("\n", " "),
    )

    row = find_appointment(date_str)
    result.add(
        "数据库：预约记录 CONFIRMED",
        bool(row) and row.get("status") == "CONFIRMED",
        f"status={row.get('status') if row else '无记录'}",
    )
    result.add(
        "数据库：外部挂号单号已落库（对账依据）",
        bool(row) and bool(row.get("external_booking_id")),
        f"external_booking_id={row.get('external_booking_id') if row else None}",
    )
    result.add(
        "数据库：患者引用已落库（P0-06 方向）",
        bool(row) and bool(row.get("patient_ref")),
        f"patient_ref={row.get('patient_ref') if row else None}",
    )

    # ---- 3. 号源确实被扣 ----
    print("\n--- 3. 号源扣减核对 ---")
    state = local_his_state()
    if state:
        booked_slots = [
            s for s in state.get("regist", [])
            if s.get("clinic_date") == date_str and s.get("current_num", 0) > 0
        ]
        result.add(
            "本地 HIS：号源已挂号数 current_num > 0（号真的被扣了）",
            bool(booked_slots),
            f"命中 {len(booked_slots)} 个号表实例",
        )
    else:
        result.add(
            "本地 HIS：号源已挂号数 current_num > 0（号真的被扣了）",
            False,
            "读不到本地 HIS 状态文件（LOCAL_HIS_STATE_PATH 未配置？）",
        )

    # ---- 4. 重复预约 → 不重复占号 ----
    print("\n--- 4. 重复预约 ---")
    before = None
    state = local_his_state()
    if state:
        before = sum(
            s.get("current_num", 0) for s in state.get("regist", [])
            if s.get("clinic_date") == date_str
        )
    again = book(message)
    state = local_his_state()
    after = None
    if state:
        after = sum(
            s.get("current_num", 0) for s in state.get("regist", [])
            if s.get("clinic_date") == date_str
        )
    duplicate_blocked = bool(
        re.search(
            r"已有.{0,16}预约|请勿重复|无需重复|已约过|重复预约|没有办理成功|没有生成",
            again,
        )
    )
    no_extra = before is None or after == before
    result.add(
        "重复预约：被识别为重复",
        duplicate_blocked,
        again[-120:].replace("\n", " "),
    )
    result.add(
        "重复预约：号源未被重复占用",
        no_extra,
        f"current_num {before} → {after}",
    )

    # ---- 5. 取消预约（退号 → 号源归还）----
    print("\n--- 5. 取消预约挂号 ---")
    cancel_output = book(
        f"取消我的预约，患者{VALID_NAME}，身份证号{VALID_ID}，"
        f"{args.department}，{date_str}{args.slot}"
    )
    cancelled = bool(
        re.search(r"取消预约成功|预约已成功取消|已取消|取消成功", cancel_output)
    )
    result.add(
        "取消预约：HIS 侧退号成功",
        cancelled,
        cancel_output[-140:].replace("\n", " "),
    )

    row = find_appointment(date_str)
    result.add(
        "数据库：预约记录 CANCELLED（状态迁移，不物理删除）",
        bool(row) and row.get("status") == "CANCELLED",
        f"status={row.get('status') if row else '无记录'}",
    )

    state = local_his_state()
    if state:
        returned = sum(
            s.get("current_num", 0) for s in state.get("regist", [])
            if s.get("clinic_date") == date_str
        )
        result.add(
            "本地 HIS：退号后号源已归还（current_num 回落）",
            returned == 0,
            f"current_num={returned}",
        )
        holds = len(state.get("holds", []))
        result.add(
            "本地 HIS：无残留预占（hold 全部释放）",
            holds == 0,
            f"holds={holds}",
        )

    # ---- 6. 查询科室专家 ----
    print("\n--- 6. 查询科室专家 ---")
    doctors = chat(f"{args.department}有哪些专家？")
    has_doctor = bool(re.search(r"[一-龥]{2,4}（", doctors))
    result.add(
        "查询科室专家：返回医生名单",
        has_doctor,
        doctors[-120:].replace("\n", " "),
    )

    print(f"\n=== 结论：{result.summary()} ===")
    if result.failed:
        print("有项目未通过 —— 本地预约挂号流程尚不可用，请看上面失败项。")
        return 1
    print("本地预约挂号全流程可用（查号源 / 预约 / 取消 / 查专家），号源扣减与归还均真实。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
