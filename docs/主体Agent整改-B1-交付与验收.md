# 主体 Agent 整改 · B1 交付与验收

> **对应方案**：`docs/主体Agent生产化架构最终整改方案.md` §4 Phase 1 / §6 批次 B1
> **前置基线**：`docs/主体Agent整改-B0-行为基线.md`（冻结提交 `94997d0`，876 passed / 6 skipped）
> **批次主题**：RunContext、统一 deadline、取消与 `finally` 收尾
> **交付状态**：代码已完成；离线回归与真机四项**已在本环境实跑通过**（用户 2026-11-12 明确授权代跑本地回归，详见 §4）。
> **后续批次**：B2 从 `EnginePhases` mixin 抽取协作者，须保持行为与事件顺序不变；本批次到此停止，不叠加 B2 改动。本批交付状态以提交记录为准。

---

## 1. 基线对照

| 项 | B0 冻结 | B1 后 |
|---|---|---|
| 提交 | `94997d0` | B1 提交（基于 `94997d0`） |
| 全量单测 | 876 passed / 6 skipped / 1 warning / 30.60s | **889 passed / 6 skipped / 1 warning / 29.60s** |
| 退出码 | 0 | 0 |
| skip 原因 | `test_live_api.py` 需 `--run-integration` | 不变 |
| warning | starlette `anyio.abc.BlockingPortal`（第三方） | 不变 |
| ruff `E9,F63,F7,F82,F811` | All checks passed | All checks passed |
| 新增用例 | — | **13 条**（`test_run_context.py`）；876 + 13 = 889 |

定点复跑：`pytest tests/unit/test_run_context.py tests/unit/test_engine_characterization.py -q` → **24 passed**（含超时预算既有用例）。

---

## 2. 变更文件与接口变化

### 2.1 文件清单

| 文件 | 类型 | 内容 |
|---|---|---|
| `xiaozhi/runtime/run_context.py` | **新增** | `RunContext` / `CancellationScope` / `ExecutionBudget` |
| `tests/unit/test_run_context.py` | **新增** | 13 条护栏单测（纯 fake，离线） |
| `xiaozhi/runtime/engine.py` | 修改 | `run()` 收敛为生命周期壳；原编排逐字移入 `_run_body()` |
| `xiaozhi/runtime/engine_phases.py` | 修改 | `_run_loop` / `_time_budget_exceeded` 增加可选 `budget` |
| `xiaozhi/reliability/tracing.py` | 修改 | 新增 `peek_trace_id()` / `restore_trace_id()` |
| `docs/主体Agent整改-B0-行为基线.md` | **新增** | B0 基线冻结文档 |
| `docs/python-production-baseline.md` | 修改 | 仅头部加 3 行指向新基线的注记，历史正文未改 |

**无数据库迁移、无配置变更、无 `.env` 变更、无依赖变更、无前端变更。**

### 2.2 新增接口

```python
RunContext.create(*, trace_id, session_id, subject_id="anonymous",
                  timeout_seconds=None, now=None) -> RunContext
# 只读属性：timeout_seconds / elapsed_seconds / remaining_seconds / expired
# as_span_attributes() -> {run_id, request_id, trace_id, session_id, deadline_seconds}

ExecutionBudget(ctx)
# .context / .time_exceeded / .remaining_for(phase) / .snapshot()

CancellationScope()
# .cancel(reason)（幂等，锁住首次原因）/ .cancelled / .reason / .cancelled_at / .snapshot()

peek_trace_id() -> Optional[str]        # 只读，不隐式新建
restore_trace_id(value) -> None         # 恢复到请求进入前的状态
```

### 2.3 兼容性

| 接口 | 变化 | 兼容方式 |
|---|---|---|
| `AgentEngine.run(memory_id, user_input, user_id, *, task_idempotency_key)` | **签名不变** | 内部委托 `_run_body()`；事件序列逐字不变 |
| `EnginePhases._run_loop(..., budget=None)` | 末尾新增可选关键字参数 | 旧调用点不传即走原逻辑 |
| `EnginePhases._time_budget_exceeded(started, budget=None)` | 新增可选参数 | 不传时保留 `time.monotonic() - started > request_timeout` |
| `tracing.set_trace_id` / `get_trace_id` | **未改动** | 只新增两个只读/恢复函数 |

### 2.4 新增可观测字段

- span `http.request` 新增属性：`run_id`、`request_id`
- 新增指标：`run_finished_total`、`run_cancelled_total`、`run_unhandled_error_total`、`run_generator_closed_total`
- `logger.debug("run 收尾：outcome=%s budget=%s", ...)` 输出剩余预算与取消原因快照

---

## 3. 关键设计取舍（含本批次踩到的坑）

### 3.1 `finally` 里绝不 yield

`AgentEngine.run()` 是 async generator。客户端断开时 `StreamingResponse` 会 `aclose()` 并抛 `GeneratorExit`；此时在 `finally` 里 `yield` 会抛 `RuntimeError: async generator ignored GeneratorExit`。所以 B1 的 `finally` 只做**同步**操作：恢复 trace 上下文、记指标、写日志。

「异常路径也必须发 `AgentEndEvent` / 终态化任务」需要统一的 `RunFinalizer` 才能安全收口（要区分正常收尾、短路收尾、异常收尾且不能重复发），归入 **B2 抽取协作者** 一并做，不在本批次用 yield 硬塞进 `finally`。

> **后续纠偏（2026-11-13，B3 决策确认）**：上段「异常路径也必须发 `AgentEndEvent`」的表述**不成立**。
> `run()` 的 `finally` 不能 `yield`，`aclose()` / `GeneratorExit` 路径物理上无法补发结束事件。
> 正确目标是「异常 / 取消路径保证**持久化终态 + 明确 outcome 指标**，不保证 `AgentEndEvent`」，归入 **B3-3**；
> `RunFinalizer` 结构抽取顺延到 **B3-4**。详见 `docs/主体Agent整改-B3-调研与分批实施方案.md` §4。

### 3.2 异常仍原样抛出，不吞、不改上层语义

`except Exception` 只记录 `result.error` / `degraded` 与指标，然后 `raise`。`routes.generate()` 原有的「服务暂时异常：{type}」兜底文案行为不变。`CancelledError` 是 `BaseException` 子类，单独一个 `except` 分支，不会被 `except Exception` 吞掉。

### 3.3 取消只记录事实，不代表远端写已取消

`CancellationScope.cancel("client_cancelled")` 只说明调用方不再等待。写工具可能已经提交，业务侧仍按既有语义保留 `STARTED` → `UNKNOWN` 待对账（`recover_stale` 只标记不重试）。B1 **没有**引入 `asyncio.wait_for` 包住写工具，也没有自动重试。三种收尾原因可区分：`client_cancelled` / `generator_closed` / 未处理异常。

### 3.4 trace 上下文的坑：`get_trace_id()` 会隐式写入 ContextVar

第一版实现用 `ContextVar.set()` 返回的 token 在 `finally` 里 `reset()`，单测立刻抓到缺陷：**`get_trace_id()` 在上下文为空时会自己生成并 `set` 一个新 ID**，token 记录的是这个新值，reset 后第一次的 trace_id 仍残留，第二次 run 拿到同一条链路 ID。

修法改为「恢复到请求进入前的状态」：

```python
entry_trace_id = peek_trace_id()   # 必须在 get_trace_id() 之前
trace_id = get_trace_id()
...
finally:
    restore_trace_id(entry_trace_id)
```

也不能简单 `set(None)` 一刀切 —— HTTP 路由在 `routes.py:187` 显式 `set_trace_id()` 让响应头 `X-Trace-Id` 与 `/trace/{id}` 精确对应，清成 None 会破坏这个契约。

### 3.5 超时判定边界保持严格等价

`RunContext.expired` 刻意用 `time.monotonic() - deadline > 0.0`（严格大于），与改造前 `monotonic() - started > request_timeout` 一字对齐；`remaining_seconds` 因 `max(0.0, ...)` 会丢负号，所以 `expired` 直接读未截断差值。`test_engine_characterization.py::test_request_timeout_degrades_with_budget_hint` 保持通过。

### 3.6 `_run_body` 只是抽壳，不是拆协作者

方案要求「先抽取协作者、再迁移方法、最后删除 mixin」。B1 只把生命周期壳与编排主体分开，方法仍全部在 `EnginePhases` mixin 里，**没有**新建 `LoopController` / `ModelRunner` / `ToolCoordinator`。B2 才是拆协作者的批次，届时 `ExecutionBudget` 会作为显式依赖注入，而不是像现在这样只从 `_run_loop` 透传一个可选参数。

---

## 4. 验收清单（已本地实跑，2026-11-12）

> 用户明确授权由本环境代跑本地回归与真机验证；以下均为**实际执行结果**，不是待办建议。
> 复跑命令：`cd guiguXiaozhi_backend_py && ./.venv/bin/python -m pytest tests/ -q -ra`

| 离线检查 | 实测结果 |
|---|---|
| `pytest tests/ -q -ra` | **889 passed / 6 skipped / 1 warning / 30.38s / EXIT=0**（skip 仍为 `test_live_api.py` 需 `--run-integration`；warning 为 starlette 第三方） |
| `pytest tests/unit/test_run_context.py -q` | 13 passed |
| `pytest tests/unit/test_run_context.py tests/unit/test_engine_characterization.py -q` | 24 passed |
| `ruff check xiaozhi tests --select E9,F63,F7,F82,F811` | All checks passed |

### 4.1 真机四项实测（8082 隔离实例）

**环境**：8081 上改动前的既有进程未触碰；另起 `APP_ENV=development ./.venv/bin/python run.py --port 8082` 加载**新代码**，认证保持开启（`.env` 的 `AUTH_ENABLED=true`），用 `ops-admin` bootstrap token 换 JWT 并经 `/v1/sessions` 建会话。启动日志确认 RAG 33 块 / 4 文件、本地 HIS 替身、审批闭环、模型 `qwen2.5:7b` 全部就绪。验证脚本为 `/tmp` 下的临时文件，不入仓库。

| 项 | 实测证据 | 结论 |
|---|---|---|
| **V1 事件顺序** | 快路径 `['agent_start','text_response','agent_end']`；真实模型链路 `['agent_start','retrieval','turn_start','text_response'×5,'agent_end']`（118.01s）。两条路径都是 `agent_start` 首、`agent_end` 末、各恰好一次 | PASS |
| **V2 trace 属性** | 响应头 `X-Trace-Id` 与 `/trace/{id}` 树 `trace_id` 一致；`http.request` span 含 `run_id=run-9c32883a…` / `request_id=req-d2c662b6…`，真实模型链路同样含 `run-cc18a790…` / `req-36022a68…` | PASS |
| **V3 客户端中断** | 读到首个事件即关闭连接；新增日志无 `GeneratorExit` / `async generator ignored` / `RuntimeError` / `Traceback` / `Exception in ASGI`；`run_cancelled_total +1`、`run_finished_total +1`，全量日志 0 处 bad marker | PASS |
| **V4 无串扰** | 串行两次、并发两次：trace_id 与 run_id 两两不同，各自事件序列完整，`/trace` 归属正确 | PASS |

**收尾核查**：`/health` → `inflight_requests=0`；`/metrics` → `requests_total=9`、`run_finished_total=9`（每个请求 finally 都收口，含短路与中断）、`run_cancelled_total=1`、`degraded_total=0`、`errors_total=0`；熔断器 `state=closed`。

**口径说明**：HTTP 层客户端断开走的是 `CancelledError` 路径（`run_cancelled_total`），不是 `GeneratorExit` / `aclose` 路径（`run_generator_closed_total` 未增长）；后者由进程内单测 `test_generator_close_marks_cancellation_without_yielding` 覆盖。**两条路径都已验证**，均不产生异常日志。

**观察项（不在本批范围）**：`请问医院门诊几点开始挂号？` 在 0.06s 内被既有的预约科室确定性守卫短路，把「问医院门诊」识别成科室名并回复「本院没有【问医院门诊】这个科室」。这是 B1 之前就存在的守卫误判，与本次改动无关；按批次纪律不顺手修，记为后续观察项。

### 4.2 新增用例覆盖点

| 组 | 条数 | 覆盖 |
|---|---:|---|
| RunContext / Budget / Cancellation 纯对象 | 6 | ID 唯一、绝对 deadline、各阶段同一份剩余预算、过期 / 不限时、非法 timeout、取消幂等 |
| 引擎级行为等价 | 2 | 正常路径事件序列不变、问候短路仍恰好一个 `AgentEndEvent` |
| 生命周期收尾 | 3 | trace 上下文不串扰、未处理异常原样抛出且清理、取消被记录并原样抛出 |
| 断开与并发 | 2 | `aclose()` 安全收尾、同一 engine 并发两请求事件各自完整 |

---

## 5. 本批次明确未做（残留与下一批接线点）

| 残留 | 归属 |
|---|---|
| 异常 / 取消路径仍不发 `AgentEndEvent`，`task_tracker.finish()` 也不会执行 | 持久化终态归 **B3-3**；`AgentEndEvent` 不补发（物理不可行），`RunFinalizer` 结构抽取归 **B3-4** |
| `TIMEOUT_BEFORE_EXECUTION` 与 `TIMEOUT_OUTCOME_UNKNOWN` 未区分；`Tool.timed_call` 仍只是耗时统计 | Phase 3 / B7 |
| 模型轮数、工具调用数、纠正重试数仍在 `TurnState`，`ExecutionBudget` 目前只拥有时间预算 | **B2** 拆协作者时迁移计数 owner |
| `run_id` / `request_id` 尚未贯穿 Message / Persister / Approval / HIS，只进了 span | Phase 2 / 3 |
| `business_idempotency_key` 三类键语义仍分裂（见 B0 §5） | Phase 3 |
| 阻塞 IO（Mongo / MySQL / Milvus / HIS / TaskTracker 同步调用）未治理 | **B3** |
| 画像抽取后台任务、消息 ID 去重、摘要 System 提权等 | Phase 2 / 4 对应批次 |

**身份与权限（B9 / Phase 5）仍后置**，但本批未放宽任何既有安全控制：最小权限检查、审批 `canonical_args`、一次性 token、production fail-closed 门禁全部原样保留。

---

## 6. 停止点

按方案 §6 批次纪律，原本要求「不由代码修改者替代用户本地回归」；本轮用户明确授权代跑，故 §4 的离线回归与真机四项已在本环境实跑并全部通过，结果如实记录在上，未由代码修改者自行签发「通过」以外的结论。

**剩余唯一动作**：把本批合并到远程 `main`（工作区改动尚未提交，含 4 个修改文件与 4 个新增文件）。合并后再推进 **B2：从 `EnginePhases` mixin 抽取 `LoopController` / `ModelRunner` / `ToolCoordinator`**，硬约束是 B0 §2.1 记录的事件顺序兼容（尤其三条短路分支的收尾位置）。

若在合并后的环境回归发现行为差异，先修 B1，不叠加 B2 的结构性改动。

---

*交付时间：2026-11-12｜基线：`94997d0`｜离线回归：889 passed / 6 skipped｜ruff：All checks passed｜真机四项（8082）：V1/V2/V3/V4 全 PASS*
