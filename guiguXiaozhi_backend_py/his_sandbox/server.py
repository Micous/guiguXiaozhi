"""HIS 沙箱 HTTP 服务 —— 对齐 参考项目/his 的协议，补齐它缺的 4 类接口。

**协议**（与 ``xiaozhi/service/his_client.py`` 逐字段对齐，Python 侧零改代码）

* 统一响应包 ``R{code, msg, data}``，``code == 200`` 才是成功；
  **HTTP 200 但 code≠200 = 业务拒绝**（号满 / 患者不存在 / 查无此单）。
* Sa-Token 风格：请求头 ``satoken`` 或 ``Authorization: Bearer <token>``；
  ``/login``、``/actuator/**`` 免认证（对应参考项目的网关白名单）。
* 写操作幂等：读 ``X-Idempotency-Key``，落 ``his_idempotency`` 表。

**7 类接口**

===  ====================================  ====================================
#   端点                                    参考项目状态
===  ====================================  ====================================
1    ``GET  /clinicForRegist/list``        已有（但无剩余号字段，这里补上）
2    ``POST /clinicForRegist/hold``        **缺失**（原项目无独立预占）
3    ``POST /currentRegister/add``         已有（原项目是"先扣号再落单"组合动作）
4    ``POST /currentRegister/cancel``      **缺失**（无退号、删单不回补号源）
5    ``GET  /currentRegister/{id}``        已有（原项目走 /clinicMaster/list）
6    ``GET  /patMasterIndex/list``         已有（这里改成不存明文身份证）
7    ``GET  /dept/list?deptCode=``         部分（原项目不支持按编码查）
===  ====================================  ====================================

**故障注入**（真机联调用，覆盖超时 / 网络波动 / 各类报错）

请求头 ``X-His-Fault`` 或环境变量 ``HIS_SANDBOX_FAULT``。

*pre-route*（业务**尚未**执行，模拟请求没被处理）：
  ``reject_hold`` / ``http429`` / ``auth_fail`` / ``system_busy`` /
  ``malformed_json`` / ``oversize`` / ``redirect`` / ``connection_reset`` /
  ``slow_drip`` / ``flaky``（前 2 次 500，之后恢复）。

*post-route*（业务**已经提交**，响应出问题 —— 最危险的情形）：
  ``timeout``（提交后不响应）/ ``http500`` / ``business_fail``。

``timeout`` 是"先提交业务再不响应"——模拟"HIS 其实已经落单，但网络断了"。
只有这样对账才有东西可收敛，才能验证本项目最关键的 UNKNOWN 语义。
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from his_sandbox.config import SandboxConfig
from his_sandbox.seed import seed
from his_sandbox.store import HisStore, SandboxReject

logger = logging.getLogger(__name__)

SUCCESS = 200
FAIL = 500

#: 免认证路径（对齐参考项目网关白名单 + 沙箱运维端点）
PUBLIC_PATHS = ("/login", "/actuator/", "/favicon.ico")


class HisSandboxHandler(BaseHTTPRequestHandler):
    config: SandboxConfig
    store: HisStore
    fault_delay: float = 5.0
    max_body_bytes: int = 256 * 1024
    # flaky 故障的命中计数（跨请求，类属性 + 锁）
    flaky_hits: int = 0
    flaky_lock = threading.Lock()
    timeout_once_hits: int = 0
    timeout_once_lock = threading.Lock()

    # ---------- 基础工具 ----------

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("HIS %s - %s", self.address_string(), fmt % args)

    def _send_raw(
        self, http_code: int, body: bytes, content_type: str = "application/json"
    ) -> None:
        self.send_response(http_code)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, http_code: int, payload: Dict[str, Any]) -> None:
        self._send_raw(
            http_code,
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
        )

    @staticmethod
    def ok(data: Any, msg: str = "ok") -> Dict[str, Any]:
        return {"code": SUCCESS, "msg": msg, "data": data}

    @staticmethod
    def fail(message: str, code: int = FAIL) -> Dict[str, Any]:
        return {"code": code, "msg": message, "data": None}

    def _body(self) -> Dict[str, Any]:
        """读请求体：长度非法 / 超限 / 非 JSON 都明确拒绝，不让畸形输入进业务层。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError) as exc:
            raise SandboxReject(400, "Content-Length 非法") from exc
        if length < 0 or length > self.max_body_bytes:
            raise SandboxReject(413, "请求体过大")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SandboxReject(400, "请求体不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise SandboxReject(400, "请求体必须是 JSON 对象")
        return payload

    def _authorized(self, path: str) -> bool:
        if not self.config.token:
            return True
        if any(path.startswith(p) for p in PUBLIC_PATHS):
            return True
        token = self.headers.get("satoken") or ""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = token or auth[7:].strip()
        return token == self.config.token

    def _fault(self) -> str:
        return (
            self.headers.get("X-His-Fault")
            or self.config.fault
            or ""
        ).strip().lower()

    # ---------- 请求分发 ----------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        fault = self._fault()

        if not self._authorized(path):
            self._send(200, self.fail("未登录或令牌无效", 401))
            return

        # 免认证的公共端点
        if path.startswith("/actuator/"):
            self._send(200, self.ok({"status": "UP", "service": "his-sandbox"}))
            return
        if path == "/login":
            self._send(
                200,
                self.ok({"access_token": self.config.token, "tokenName": "satoken"}),
            )
            return

        # ---- pre-route 故障：业务还没执行，请求确定没被处理 ----
        if self._pre_route_fault(fault, path):
            return

        try:
            body = self._body() if method in ("POST", "PUT") else {}
            idem = (
                self.headers.get("X-Idempotency-Key")
                or str(body.get("idempotencyKey") or "")
            )
            data = self._route(method, path, params, body, idem)
        except SandboxReject as exc:
            self._send(200, self.fail(exc.message, exc.code))
            return
        except Exception as exc:  # 沙箱自身异常也要能定位，不能静默吞
            logger.exception("HIS 沙箱内部错误：%s %s", method, path)
            self._send(200, self.fail(f"HIS 内部错误：{exc}"))
            return

        # ---- post-route 故障：业务**已经提交**，只是响应出问题 ----
        if self._post_route_fault(fault, data):
            return
        self._send(200, self.ok(data))

    # ---------- 故障注入 ----------

    def _pre_route_fault(self, fault: str, path: str) -> bool:
        """返回 True 表示已自行发出响应，主流程应终止。"""
        if fault == "reject_hold" and path.endswith("/hold"):
            self._send(200, self.fail("该时段号源已全部约满（故障注入）"))
            return True
        if fault == "http429":
            self._send(429, self.fail("请求过于频繁（故障注入）", 429))
            return True
        if fault == "auth_fail":
            # 院方网关普遍把鉴权失败包进 R 包，而不是真的回 HTTP 401
            self._send(200, self.fail("令牌已过期，请重新登录", 401))
            return True
        if fault == "system_busy":
            # HTTP 200 + 系统类业务码：最容易被误判成"明确失败"的情形
            self._send(200, self.fail("数据库超时，请重试", 503))
            return True
        if fault == "pii_leak":
            # 错误正文夹带 PII 与凭据：验证客户端不会把它们回给用户/历史
            self._send(
                200,
                self.fail(
                    "患者 110101199003074567 信息校验失败，token=abc123secret", 500
                ),
            )
            return True
        if fault == "malformed_json":
            self._send_raw(200, b"<html><h1>502 Bad Gateway</h1></html>", "text/html")
            return True
        if fault == "oversize":
            self._send_raw(
                200,
                b'{"code":200,"msg":"ok","data":"' + b"x" * (2 << 20) + b'"}',
            )
            return True
        if fault == "redirect":
            self.send_response(302)
            self.send_header(
                "Location", "http://127.0.0.1:18081/actuator/health"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True
        if fault == "connection_reset":
            # 直接断连：客户端看到连接中途断开 → 结果不确定
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            self.close_connection = True
            return True
        if fault == "slow_drip":
            self._slow_drip()
            return True
        if fault == "flaky":
            with self.flaky_lock:
                HisSandboxHandler.flaky_hits += 1
                hits = HisSandboxHandler.flaky_hits
            if hits <= 2:
                self._send(500, self.fail(f"网关瞬时抖动 {hits}/2（故障注入）"))
                return True
        return False

    def _slow_drip(self) -> None:
        """每隔 0.12s 吐 512B：单次 socket 超时永远不触发，只有总时限能拦住。"""
        body = b'{"code":200,"msg":"ok","data":"' + b"x" * 65536 + b'"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            for i in range(0, len(body), 512):
                self.wfile.write(body[i : i + 512])
                self.wfile.flush()
                time.sleep(0.12)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # 客户端按整体 deadline 断开，正是预期结果

    def _post_route_fault(self, fault: str, data: Any) -> bool:
        """返回 True 表示已自行发出（或吞掉）响应，主流程应终止。"""
        if fault == "timeout":
            # 业务已提交，只是不回响应 —— 模拟"HIS 已落单但网络断了"
            time.sleep(self.fault_delay)
            return True
        if fault == "timeout_once":
            # 只丢第一次响应：第二次（同幂等键）应拿到首次结果 → 验证
            # "网络波动靠幂等重放收敛"，而不是一律推给 UNKNOWN
            with self.timeout_once_lock:
                HisSandboxHandler.timeout_once_hits += 1
                hits = HisSandboxHandler.timeout_once_hits
            if hits == 1:
                time.sleep(self.fault_delay)
                return True
            return False
        if fault == "http500":
            self._send(500, self.fail("HIS 服务端错误（故障注入）"))
            return True
        if fault == "business_fail":
            self._send(200, self.fail("号源已全部约满（业务故障注入）"))
            return True
        return False

    # ---------- 路由 ----------

    def _route(
        self,
        method: str,
        path: str,
        params: Dict[str, str],
        body: Dict[str, Any],
        idem: str,
    ) -> Any:
        store = self.store

        # === 1. 号源查询 ===
        if method == "GET" and path == "/clinicForRegist/list":
            return store.query_slots(params)
        if method == "GET" and path.startswith("/clinicForRegist/") and path.count("/") == 2:
            slot_id = path.rsplit("/", 1)[-1]
            slot = store.query_slot_by_id(slot_id)
            if slot is None:
                raise SandboxReject(404, f"号表实例不存在：{slot_id}")
            return slot

        # === 2. 预占（参考项目缺失） ===
        if method == "POST" and path == "/clinicForRegist/hold":
            return store.hold_slot(body, idem)

        # === 预占释放 ===
        if method == "POST" and path == "/clinicForRegist/release":
            return store.release_hold(str(body.get("holdId") or ""), idem)

        # === 3. 落单（确认预占） ===
        if method == "POST" and path == "/currentRegister/add":
            return store.confirm_hold(body, idem)

        # === 4. 退号（参考项目缺失） ===
        if method == "POST" and path == "/currentRegister/cancel":
            return store.cancel_booking(str(body.get("clinicMasterId") or ""), idem)

        # === 5. 查单 ===
        # 按幂等键查单：解开「落单超时且没拿到外部单号」的对账死结
        if method == "GET" and path.startswith("/currentRegister/byKey/"):
            return store.query_booking_by_idempotency_key(
                path.rsplit("/", 1)[-1]
            )
        if method == "GET" and path.startswith("/currentRegister/") and path.count("/") == 2:
            return store.query_booking(path.rsplit("/", 1)[-1])
        if method == "GET" and path == "/clinicMaster/list":
            return store.list_bookings(params)

        # === 6. 患者主档 ===
        if method == "GET" and path == "/patMasterIndex/list":
            return store.query_patients(params)
        if method == "POST" and path in ("/patMasterIndex", "/patMasterIndex/add"):
            return store.create_patient(body)

        # === 7. 院方主档：科室字典 + 医生名录 ===
        if method == "GET" and path == "/dept/list":
            return store.query_depts(params)
        if method == "GET" and path == "/doctor/list":
            return store.query_doctors(params)
        if method == "GET" and path.startswith("/dept/") and path.count("/") == 2:
            dept_id = path.rsplit("/", 1)[-1]
            rows = store.query_depts({})
            for row in rows:
                if row["deptId"] == dept_id:
                    return row
            raise SandboxReject(404, f"科室不存在：{dept_id}")

        # === 运维（需认证） ===
        if method == "GET" and path == "/_admin/stats":
            return store.stats()
        if method == "POST" and path == "/_admin/reset":
            result = seed(store, self.config, reset=True)
            return {"reset": True, **result}
        if method == "POST" and path == "/_admin/reset-fault":
            with self.flaky_lock:
                HisSandboxHandler.flaky_hits = 0
            with self.timeout_once_lock:
                HisSandboxHandler.timeout_once_hits = 0
            return {"reset": True}

        raise SandboxReject(404, f"未知端点：{method} {path}")


def create_server(
    config: Optional[SandboxConfig] = None,
    *,
    auto_seed: bool = True,
) -> Tuple[ThreadingHTTPServer, SandboxConfig]:
    config = config or SandboxConfig.from_env()
    store = HisStore(config)
    store.init_schema()
    if auto_seed:
        result = seed(store, config)
        logger.info("HIS 沙箱演示数据：%s", result)
    handler = type(
        "BoundHisSandboxHandler",
        (HisSandboxHandler,),
        {"config": config, "store": store},
    )
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.daemon_threads = True
    return server, config


def main() -> int:
    parser = argparse.ArgumentParser(description="HIS 本地沙箱服务")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-seed", action="store_true", help="不重新灌演示数据")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.port:
        import os

        os.environ["HIS_SANDBOX_PORT"] = str(args.port)
    server, config = create_server(auto_seed=not args.no_seed)
    logger.info(
        "HIS 沙箱已启动：http://%s:%d  (token=%s, db=%s:%d/%s)",
        config.host, config.port, config.token,
        config.mysql_host, config.mysql_port, config.mysql_database,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
