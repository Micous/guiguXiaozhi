# 主体 Agent 整改 · B3-1 交付与验收：有界阻塞 IO 执行器

> **冻结基线**：`c578879`（B3 决策文档批次，914 passed / 6 skipped）
> **交付时间**：2026-11-13
> **文档性质**：Phase 3 / 批次 B3-1 交付物。**本批次不改对外行为**（返回值、异常类型、事件序列、
> 超时语义全部不变），只把 async 链路里的同步阻塞 IO 换到「有界、可观测、可排空」的专用执行器上。

---

## 1. 范围与结论

方案 §3 批次表对 B3-1 的要求：新增有界、可观测的专用 IO 执行器（带队列上限、指标、
shutdown 排空）；把 `memory.store` 的同步 Mongo、`task_repo` 的同步 MySQL、`his_client`
的同步 HTTP 统一经它调度。本批次全部完成，另有两处边界明确收敛（§6）。

**结论**：离线回归 `926 passed / 6 skipped`（914 基线 + 本批 12 条新护栏），
真机 8082 隔离实例 B1 四项 + B2 四项回归 + B3-1 四项专项全部 PASS（§7）。

---

## 2. 核心交付：`xiaozhi/ops/bounded_io.py`

### 2.1 `BoundedIOExecutor`

```python
executor = BoundedIOExecutor(max_workers=8, max_inflight=64, metrics=metrics)
result = await executor.run(fn, *args, **kwargs)     # 返回值/异常原样传递
await executor.aclose(drain_timeout=10.0)            # 停收 → 排空 → 关池
```

| 约束 | 实现 |
|---|---|
| 契约与 `asyncio.to_thread` 一致 | 返回值、异常**对象**原样传递（不包装）；`fn` 用 positional-only `/` 接收，业务自带的 `timeout` kwarg 原样透传 |
| contextvars 不丢 | `loop.run_in_executor` 默认不传播，这里显式 `contextvars.copy_context().run(call)`，trace / approval 上下文完整穿透线程池 |
| 有界性 | 两层：`ThreadPoolExecutor(max_workers)` 限真实线程数；`asyncio.Semaphore(max_inflight)` 限「执行中 + 排队中」 |
| **不静默丢弃** | 超限任务在 async 层排队等待，不抛错、不丢弃；显式拒绝策略属行为变更，留给后续灰度 |
| 每 loop 独立信号量 | 3.11 的 `asyncio.Semaphore` 绑定 loop；按 `AbstractEventLoop` 分别持有，线程池与计数器跨 loop 共享（pytest 每用例一个 loop 也不会炸） |
| shutdown 排空 | `aclose()`：置 `_closed` → 等 `_drained` 事件（`drain_timeout`）→ `pool.shutdown(wait=True)`；幂等；关闭后新提交抛 `IOExecutorClosed`（显式失败，绝不偷偷吞掉） |

### 2.2 进程级装配钩子

```python
configure_bounded_io(executor)   # 组合根装配（带指标的实例）
get_bounded_io()                 # 未装配时 lazy 建裸默认实例（离线单测零改动）
release_bounded_io(executor)     # 仅当全局仍指向该实例才清空
run_sync_io(fn, *args, **kw)     # async 调用方的一行式便捷入口
```

`release_bounded_io` 是必须的：应用可能在单进程内多次装配 / 关闭（测试、热重载）。
若直接置 `None` 会误伤后一次装配的新实例；若不清空，下一次请求会拿到已 `aclose()`
的执行器并抛 `IOExecutorClosed`。

### 2.3 新增配置项（`config.py`）

```python
io_max_workers: int = 8            # 真实线程数上限
io_max_inflight: int = 64          # 执行中 + 排队中 上限
io_drain_timeout_seconds: float = 10.0   # 停机排空等待
```

### 2.4 新增指标（`/metrics` snapshot）

| 名称 | 类型 | 含义 |
|---|---|---|
| `io_executor_submitted_total` | counter | 提交进执行器的任务数 |
| `io_executor_completed_total` | counter | 成功完成 |
| `io_executor_failed_total` | counter | 抛异常 / 本地等待超时 |
| `io_executor_timeout_total` | counter | 显式 `wait_timeout` 触发次数 |
| `io_executor_closed_total` | counter | `aclose()` 次数 |
| `io_executor_inflight` | gauge | 当前「执行中 + 排队中」，收尾必须归零 |
| `io_executor_duration_ms` | histogram | 单任务在执行器里的总耗时 |
| `io_executor_queue_wait_ms` | histogram | 拿槽位前的排队等待 |

---

## 3. 迁移点位清单（共 25 处）

| 文件 | 点位 | 说明 |
|---|---|---|
| `memory/store.py` | `ping` / `load` / `save`（CAS）/ `clear` | 原 `asyncio.to_thread` + 3 处直接同步 pymongo；**此前只有探活是异步的**，读写清空都在卡 event loop |
| `ops/lifecycle.py` | HealthChecker 同步探针 | 原 `asyncio.to_thread`，换有界执行器 |
| `ops/redis_limiter.py` | `allow_async` | 原 `asyncio.to_thread`，换有界执行器 |
| `application.py` | `session_repo.ensure_table` / `task_repo.ensure_tables` / `task_repo.recover_stale` / `mongo_sink.ping` | 启动期 4 处同步外部 IO（MySQL ×3、Mongo ping）；移出 event loop |
| `application.py` | 装配 | `configure_bounded_io(io_executor)` 在任何组件之前；`Application.io_executor`；`aclose()` 最后排空并 `release_bounded_io` |
| `runtime/engine.py` | `department_provider()` / `task_tracker.begin` | 科室守卫的主档读取 + 任务登记，均为同步 HIS / MySQL |
| `runtime/engine_phases.py` | `tool_started` / `tool_finished` / `finish` | 审批兜底执行路径 + `_finalize` 终态化 |
| `runtime/tool_coordinator.py` | `tool_started` / `tool_finished` | 主工具执行路径（B2 第三批协作者） |
| `tools/appointment_tools.py` | `list_departments` ×2、`list_doctors` ×2、`book`、`cancel`、`slot_service.check` | 7 处工具内同步服务调用；`_detect_name_swap` 随之转为 async（调用点仅 1 处） |
| `service/his_client.py` | 新增 `request_async` | 见 §4 |

> 命名刻意避开 `timeout`：执行器的等待限界参数叫 `wait_timeout`，业务函数（如
> `HisClient.request`）自带的 `timeout` 必须能原样透传 —— 这是 `run(fn, /, *args, **kwargs)`
> 签名设计的原因，已有单测钉住。

---

## 4. `HisClient.request_async`：`time.sleep` 退避的等价实现

方案原文写「`time.sleep` 退避换 `await asyncio.sleep`」。直接改 `request()` 签名会破坏
同步契约并波及全部服务层与既有用例，不符合 B3-1「行为不变」硬约束。本批的实现：

```python
async def request_async(self, method, path, *, params=None, body=None,
                        idempotency_key=None, timeout=None) -> HisResponse:
    return await get_bounded_io().run(
        self.request, method, path, params=params, body=body,
        idempotency_key=idempotency_key, timeout=timeout,
    )
```

把**整段同步重试循环（含 `time.sleep(next_delay)`）**一次性 offload 到有界线程池：

* 在线程里 sleep 只占一个工作线程，事件循环不再被卡；
* 重试 deadline、熔断、幂等键、异常分类逐字不变（`CircuitBreaker` 内部已有 `threading.Lock`，线程安全）；
* 既有 `request()` 与全部同步调用方零改动。

工具链路的实际 offload 落点在**服务层边界**（`appointment_tools` 的 7 处
`await run_sync_io(...)`），`request_async` 是 HIS 专用的显式异步入口，二者共用同一执行器。

---

## 5. 等价性判据与单测（`tests/unit/test_bounded_io.py`，12 条）

方案 §3.1 四条判据 → 对应用例：

| 判据 | 用例 |
|---|---|
| 同一输入下返回值 / 异常对象 / kwargs 逐字一致 | `test_direct_call_and_offloaded_call_are_equivalent`（含业务自带 `timeout` kwarg）、`test_exception_is_the_same_object`（同一异常对象，不包装）、`test_his_request_async_is_equivalent_and_offloaded` |
| 阻塞不卡 event loop、contextvars 不丢 | `test_blocking_call_does_not_stall_event_loop`、`test_contextvars_are_propagated_to_worker`、`test_his_request_async_...`（断言在非主线程执行） |
| 队列满不静默丢弃、真实并发受 `max_workers` 限制 | `test_queue_saturation_never_drops_tasks`（12 任务 / inflight=3 全部完成）、`test_max_workers_caps_real_concurrency`（峰值 ≤ 2） |
| shutdown 排空、不留无主任务 | `test_aclose_drains_and_rejects_new_submissions`、`test_wait_timeout_raises_and_accounts_for_failure`、`test_aclose_is_idempotent`（并入前者） |
| 可观测性 / 装配正确性 | `test_metrics_and_snapshot_are_observable`、`test_run_sync_io_uses_configured_instance`、`test_second_event_loop_gets_its_own_semaphore` |

```text
12 passed in 0.92s        # 定点
926 passed / 6 skipped / 1 warning / 30.78s / EXIT=0   # 全量（基线 914 + 12）
```

---

## 6. 明确不做（边界收敛）

1. **`prevalidate_args` / `build_department_hint`**：L0 审批门内与工具构造期的同步调用，
   改造需要把 `Tool.prevalidate_args` 变 async（接口级变更、波及 4 个工具与 secure_executor），
   不属于「行为不变」批次，归入 B3-5 / 后续批次。
2. **`profile/store`、`profile/vector`、`ops/audit` 的同步 IO**：方案 §2.2 全量清单里的其余项，
   本批不扩 —— audit 有 fail-closed 语义，须与 B3-2 的审计语义变更一起评估。
3. **异步客户端替换（`urllib` / `mysql.connector` / `pymongo`）**：方案 §3 已顺延为 B3-5，
   若有界执行器达标可不做。
4. **显式拒绝策略（队列满抛错）**：属于行为变更，本批只做「排队不丢弃」。

---

## 7. 真机验收（8082 隔离实例，认证开启）

复用 B2 正式验收脚本（R1–R4 / V1–V4）+ B3-1 专项四项，实测结果：

| 项 | 内容 | 结果 |
|---|---|---|
| R1 事件顺序 | 问候快路径 `agent_start → text_response → agent_end`，各恰好一次 | **PASS** |
| R2 trace ids | `/trace/{id}` 树匹配 `X-Trace-Id`，`http.request` 带 `run_id=run-…` / `request_id=req-…` | **PASS** |
| R3 客户端中断 | `interrupted=True`，`run_cancelled_total` +1，无 `GeneratorExit` / `Traceback` | **PASS** |
| R4 无串扰 | 串行 + 并发各两条，trace_id / run_id 两两不同、事件序列完整 | **PASS** |
| V1 短路 | 问候路径 spans 无 `llm.chat` | **PASS** |
| V2 工具链路 | 见下方「V2 波动说明」 | **PASS（复测）** |
| V3 日志 | 全程 22 行新日志，无 `GeneratorExit` / `Traceback` / `Exception in ASGI` | **PASS** |
| V4 收尾 | `inflight=0`、`requests_total == run_finished_total`、`errors_total=0` | **PASS** |
| V5 执行器指标 | submitted=39 / completed=37 / failed=2 / inflight gauge=0；duration / queue_wait 直方图有样本；账目守恒 | **PASS** |
| V6 链路确证 | 真实「查询号源」请求 `io_executor_submitted_total` +5，事件含 `tool_execute → tool_result` | **PASS** |
| V7 启动与日志 | 「有界 IO 执行器已装配（workers=8 inflight=64）」，无 `IOExecutorClosed` 等坏标记 | **PASS** |
| V8 loop 响应 | 工具链路进行中 385 次 `/health` 探活，最差 0.995s，零错误 | **PASS** |

### V2 波动说明（非代码回归）

同一话术在**已累积多条问候历史的老会话**里连续两次被模型直接文字回答
（`tool_call_count=0`，`prompt_tokens=2749`），未进入工具链路。用**全新 session**
复测同一话术，第一次即完整复现 B2 冻结的事件序列：

```text
events=['agent_start', 'turn_start', 'text_response', 'tool_execute',
        'tool_result', 'turn_end', 'turn_start', 'text_response', 'agent_end']
spans=['context.assemble', 'llm.chat', 'tool.execute', 'llm.chat',
       'memory.write', 'http.request', 'memory.load']
```

与 B2 第二/三批交付时记录的快照逐字一致 —— 判定为 7B 模型的非确定性 +
会话历史影响，与 B3-1 改动无关（V6 亦在同批次内提供了完整工具链路证据）。

### 指标语义澄清：`io_executor_failed_total` 含「调用方取消」

R3 客户端断开会取消一个在途执行器任务，`await` 抛 `CancelledError` → 记一次
`failed`（本轮 2 次 failed 均来自两次验收里的客户端中断，`failed <= run_cancelled_total`，
且全程无任何 WARNING / ERROR）。验收判据因此用「账目守恒
（submitted == completed + failed）+ failed ≤ 取消数」，而非 `failed == 0`。

原始输出：`/tmp/b3_1_verify_output.log`、`/tmp/b3_1_verify_result.json`、
`/tmp/b3_1_regression_result.json`、`/tmp/b3_1_v2_retry_result.json`。

---

## 8. 下一批（B3-2）入口

* 收敛 `WRITE_TOOL_NAMES` 单一常量（`state.py` 与 `task_tracker.py` 目前重复定义 `_WRITE_TOOLS`）；
* `TaskTracker.tool_started()` 返回 `DISABLED / RECORDED / FAILED` 三态（现在把「未装配」和
  「落库失败」都吞成 `None`）；
* `require_started_for_writes` 显式策略（默认 false）+ 组合根 WARNING + 启动期一致性校验；
* 故障注入矩阵按方案 §3.2 执行。
