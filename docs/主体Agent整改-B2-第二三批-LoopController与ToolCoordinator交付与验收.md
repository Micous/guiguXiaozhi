# 主体 Agent 整改 · B2 第二 / 三批（LoopController / ToolCoordinator）交付与验收

> **对应方案**：`docs/主体Agent生产化架构最终整改方案.md` §3.1 / §3.2 / §4 P1-1.1（「先抽协作者、再迁移方法、最后删 mixin」）、§4 P1-1.5（事件顺序兼容）、§6 批次 B2
> **前置基线**：B2 第一批提交 `bd98e34`（`docs/主体Agent整改-B2-第一批-ModelRunner交付与验收.md`，897 passed / 6 skipped）
> **批次主题**：从 `EnginePhases` mixin 继续抽出 `LoopController`（主循环）与 `ToolCoordinator`（工具执行）两个显式协作者
> **交付状态**：代码已完成；离线回归与真机验收**已在本环境实跑通过**（延续用户 2026-11-12「你本地自己测试」的要求，详见 §6）。
> **本批边界**：只搬 `_run_loop` 与 `_execute_tools` 两个方法。三个确定性短路、`_chat_turn`（已迁 `ModelRunner`）、`_finalize`、异常路径终态化一律不动。

---

## 1. 基线对照

| 项 | B2 第一批（前置） | 第二批后 | 第二 / 三批后（本文） |
|---|---|---|---|
| 提交 | `bd98e34` | 基于 `bd98e34` | 基于 `bd98e34` |
| 全量单测 | 897 passed / 6 skipped / 29.45s | **905 passed / 6 skipped / 29.95s** | **914 passed / 6 skipped / 1 warning / 29.18s** |
| 退出码 | 0 | 0 | 0 |
| skip 原因 | `test_live_api.py` 需 `--run-integration` | 不变 | 不变 |
| warning | starlette `anyio.abc.BlockingPortal`（第三方） | 不变 | 不变 |
| ruff `E9,F63,F7,F82,F811` | All checks passed | All checks passed | All checks passed |
| 新增用例 | 8 条（`test_model_runner.py`） | **8 条**（`test_loop_controller.py`） | **+9 条**（`test_tool_coordinator.py`）；897 + 8 + 9 = 914 |
| `EnginePhases` 行数 | 1,183 | — | **954**（净减 229 行） |

定点复跑：

```text
./.venv/bin/python -m pytest tests/unit/test_loop_controller.py -q -ra
8 passed / EXIT=0

./.venv/bin/python -m pytest tests/unit/test_tool_coordinator.py -q -ra
9 passed in 0.55s / EXIT=0
```

---

## 2. 变更文件与接口变化

### 2.1 文件清单

| 文件 | 变化 |
|---|---|
| `xiaozhi/runtime/loop_controller.py` | **新增**。`LoopPorts` + `LoopController`，承载主循环（219 行） |
| `xiaozhi/runtime/tool_coordinator.py` | **新增**。`ToolCoordinator`，承载一轮工具调用的执行（226 行） |
| `xiaozhi/runtime/engine_phases.py` | `_run_loop` 的 166 行实现改为委托壳；`_execute_tools` 的 163 行实现改为委托壳；清理只被它们使用的导入（`TurnStartEvent` / `TurnEndEvent` / `_empty_turn` / `ApprovalRequiredEvent` / `restore_masked_id_cards`） |
| `xiaozhi/runtime/engine.py` | `__init__` 组装 `self._loop_controller`（注入 10 个 Port）与 `self._tool_coordinator`（注入 2 个纯函数 Port） |
| `tests/unit/test_loop_controller.py` | **新增** 8 条护栏 |
| `tests/unit/test_tool_coordinator.py` | **新增** 9 条护栏 |
| `docs/主体Agent整改-B2-第二三批-LoopController与ToolCoordinator交付与验收.md` | 本文 |

### 2.2 `LoopController`：显式 Port 集合

```python
@dataclass(frozen=True)
class LoopPorts:
    time_budget_exceeded: Callable[..., bool]
    degrade_on_time_budget: Callable[..., AsyncIterator[Event]]
    circuit_is_open: Callable[[], bool]
    degrade_on_circuit_open: Callable[..., AsyncIterator[Event]]
    degrade_on_model_error: Callable[..., AsyncIterator[Event]]
    chat_turn: Callable[..., AsyncIterator[Event]]
    execute_approved_operation: Callable[..., AsyncIterator[Event]]
    retry_with_approval_hint: Callable[..., bool]
    schedule_corrective_retry: Callable[..., bool]
    execute_tools: Callable[..., AsyncIterator[Event]]

class LoopController:
    async def run_loop(
        self, *,
        messages, tools, known_tool_names, state, user_input, user_id,
        trace_id, request_span, started, result, outcome, budget=None,
        max_turns: int, circuit_breaker,
    ) -> AsyncIterator[Event]: ...
```

`AgentEngine.__init__` 组装时把 mixin 上现存的 10 个方法直接绑成 Port —— **调用点零改动**：`execute_tools` Port 仍指向 `EnginePhases._execute_tools` 委托壳，因此第二、三批叠加后链路是
`run() → LoopController.run_loop() → _execute_tools 委托壳 → ToolCoordinator.execute_tools()`，三层透传但没有任何编排语义变化。

### 2.3 `ToolCoordinator`：纯函数 Port + 按调用传入的运行时依赖

```python
class ToolCoordinator:
    def __init__(
        self, *,
        assistant_to_ollama: Callable[[Message], Dict[str, Any]],
        tool_to_ollama: Callable[[Message], Dict[str, Any]],
    ) -> None: ...

    async def execute_tools(
        self, *,
        response, messages, state, user_id, trace_id, result,
        metrics, max_tool_calls, tool_failure_threshold,
        tool_registry, task_tracker, secure_executor,
    ) -> AsyncIterator[Event]: ...
```

`_execute_tools` 原本是 mixin 里唯一通过 `self` 调用 `engine.py` **静态方法**的方法
（`_assistant_to_ollama` / `_tool_to_ollama`）。协作者不能反向 import `AgentEngine`（循环依赖），
因此这两个消息格式转换以显式 callable Port 注入；其余 6 个运行时依赖按调用传入。

### 2.4 逐字平移的程序化证明

延续第一批的做法：用脚本从 `git show bd98e34:.../engine_phases.py` 取出旧方法体，与新协作者方法体做归一化 diff（脚本 `/tmp/b2_loop_equiv.py`、`/tmp/b2_tool_equiv.py`，临时文件不入库）：

```text
=== 主循环 ===
old body lines=146  new body lines=146
EQUIVALENT: 主循环方法体逐字一致，仅有 12 处标识符替换
  self.max_turns -> max_turns
  self.circuit_breaker -> circuit_breaker
  self._time_budget_exceeded( -> self._ports.time_budget_exceeded(
  self._degrade_on_time_budget( -> self._ports.degrade_on_time_budget(
  self._circuit_is_open( -> self._ports.circuit_is_open(
  self._degrade_on_circuit_open( -> self._ports.degrade_on_circuit_open(
  self._chat_turn( -> self._ports.chat_turn(
  self._degrade_on_model_error( -> self._ports.degrade_on_model_error(
  self._execute_approved_operation( -> self._ports.execute_approved_operation(
  self._retry_with_approval_hint( -> self._ports.retry_with_approval_hint(
  self._schedule_corrective_retry( -> self._ports.schedule_corrective_retry(
  self._execute_tools( -> self._ports.execute_tools(

=== 工具执行 ===
old body lines=152  new body lines=152
EQUIVALENT: 工具执行方法体逐字一致，仅有 6 处依赖标识符替换
  self.metrics -> metrics
  self.max_tool_calls -> max_tool_calls
  self.tool_failure_threshold -> tool_failure_threshold
  self.tool_registry -> tool_registry
  self.task_tracker -> task_tracker
  self.secure_executor -> secure_executor
```

即：298 行实现（含全部注释、全部 `yield` 时机、`break` 位置、审批门控短路、
PII 真值还原、`STARTED`/`FINISHED` 打点顺序）没有任何语义编辑。

---

## 3. 设计取舍

### 3.1 协作者实例不持有请求级状态

`AgentEngine` 是长生命周期对象，`_loop_controller` / `_tool_coordinator` 被并发请求共享。
`messages` / `state` / `result` / `outcome` / `response` 全部是调用参数，`metrics` / `tool_registry` /
`secure_executor` / `task_tracker` / `max_turns` / `circuit_breaker` 全部**按调用从委托壳现取**。
`test_loop_controller_holds_no_request_state()` 断言实例字段只有 `_ports`；
`test_tool_coordinator_holds_no_request_state()` 断言实例字段只有两个纯函数 Port。

### 3.2 为什么 `max_turns` / `circuit_breaker` 也按调用传入

既有装配与单测会在构造后替换 `engine.max_turns`、`engine.circuit_breaker`、
`engine.secure_executor`、`engine.tool_registry`。若在构造期快照，这些注入会静默失效，属于行为变更。
本批用三条测试钉住：`max_turns` 改成 0 后主循环完全不进模型、构造后赋予常开熔断器会在 `TURN_START`
之前降级、构造后替换 `secure_executor` 的结果确实出现在 `TOOL_RESULT` 事件里。

### 3.3 为什么保留两个委托壳

方案 §4 P1-1.1 的迁移顺序是「先抽协作者、再迁移方法、**最后**删 mixin」。本批仍不删：

* `LoopController` 的 `execute_tools` Port 指向 mixin 委托壳，使第三批接线无需改动 `LoopPorts`；
* 既有单测与其他调用点仍可用 `engine._run_loop(...)` / `engine._execute_tools(...)`；
* 等 B2 全部协作者抽完、`RunFinalizer` 统一异常终态后，再一次性删除 mixin。

### 3.4 为什么 `run()` 的生命周期壳完全没动

`LoopController` 只接管主循环内部，B1 的 `AgentEngine.run()` 生命周期壳（统一 deadline、取消、
trace 恢复、指标收尾）保持原样。因此 B1 的 13 条护栏、第一批的 8 条护栏全部原样通过。

---

## 4. 行为与事件顺序兼容性

### 4.1 完整事件序列快照（第二批）

| 场景 | 冻结的事件序列 |
|---|---|
| 正常问答 | `agent_start, turn_start, text_response, agent_end` |
| 问候短路 | `agent_start, text_response, agent_end` |
| 模型抛错降级 | `agent_start, turn_start, text_response, degraded, agent_end` |
| `max_turns=0` | `agent_start, agent_end`（`chat_model.calls == 0`） |
| 熔断器常开 | `agent_start, degraded, agent_end`（无 `turn_start`，`chat_model.calls == 0`） |

### 4.2 工具执行路径语义（第三批 9 条护栏）

| 场景 | 钉住的语义 |
|---|---|
| 成功执行 | `tool_execute → tool_result`；`messages` 回填 assistant + tool 两条；`state.tool_call_count`/`successful_tool_calls`；`result.tool_calls`；`tool_results` drain 后清空、落库累积不动 |
| 审批门控 | `tool_execute → text_response → approval_required`，**不再发 `tool_result`**；`result.text` / `state.final_reply_text` / `state.approval_detail` 置位 |
| 工具未注册 | 结构化失败结果仍发 `tool_result`（`is_error=True`），`error_type=ToolNotFound` 进失败账本 |
| 次数上限 | 构造后 `max_tool_calls=0`：零事件、零执行，仍回填「次数已达上限」提示消息 |
| PII 真值还原 | 掩码身份证在**执行前**还原为真值并原地更新同一份参数；执行器收到真值、回填 `messages` 也是真值、`id_card_restored_total +1` |
| Phase 5 生命周期 | `tool_started` 先于执行、`tool_finished` 收尾，`exec_id` 与工具名一致 |
| 委托等价 | 委托壳与协作者本体的事件类型、metadata、`messages`、`state`、`result` 逐字一致 |

### 4.3 既有护栏全量通过

`914 passed` 包含：B1 的 13 条、B2 第一批的 8 条、第二批的 8 条、第三批的 9 条，
以及仓库既有的全部 876 条基线用例。**没有删除或改写任何一条既有断言**。

---

## 5. 离线回归（本环境实跑）

```text
./.venv/bin/python -m pytest tests/ -q -ra
914 passed, 6 skipped, 1 warning in 29.18s / EXIT=0

./.venv/bin/python -m pytest tests/unit/test_loop_controller.py -q -ra
8 passed / EXIT=0

./.venv/bin/python -m pytest tests/unit/test_tool_coordinator.py -q -ra
9 passed in 0.55s / EXIT=0

./.venv/bin/python -m ruff check --select E9,F63,F7,F82,F811 xiaozhi/runtime/ tests/unit/
All checks passed!

./.venv/bin/python -m ruff check --select F401 \
  xiaozhi/runtime/tool_coordinator.py xiaozhi/runtime/loop_controller.py \
  xiaozhi/runtime/engine_phases.py tests/unit/test_tool_coordinator.py \
  tests/unit/test_loop_controller.py
All checks passed!
```

---

## 6. 真机验收（8082 隔离实例，认证开启；8081 用户常驻进程不触碰）

启动方式沿用 B1 / B2 第一批的隔离口径：`APP_ENV=development ./.venv/bin/python run.py --port 8082`，
认证保持 `.env` 的 `AUTH_ENABLED=true`，通过 `POST /v1/auth/token`（`ops-admin` + `AUTH_BOOTSTRAP_SECRET`）
换 JWT、`POST /v1/sessions` 建会话，`/metrics` 与 `/trace/{id}` 均带 Bearer。
本机 `curl` 连 `127.0.0.1` 会被代理接管，全部请求使用 `httpx.Client(..., trust_env=False)`。

### 6.1 B1 四项回归（确认本批没有回退既有语义）

| 项 | 结果 | 证据 |
|---|---|---|
| R1 事件顺序 | PASS | 问候快路径 `agent_start, text_response, agent_end`，首尾各恰好一次 |
| R2 trace ids | PASS | `X-Trace-Id` 与 `/trace/{id}` 对齐；`http.request` span 含 `run_id=run-29354d9f…` / `request_id=req-349c0f4b…` |
| R3 客户端中断 | PASS | 读首个事件即断开，`run_cancelled_total +1`、`run_finished_total +1`，服务日志无异常堆栈 |
| R4 连续 + 并发无串扰 | PASS | `serial_ok=True`、`parallel_ok=True`；两次请求的 trace_id / run_id 两两不同，事件序列各自完整 |

### 6.2 B2 第一批回归

V1 短路绕过协作者：问候 trace 的 spans 为
`greeting.fast_path / memory.write / http.request / memory.load`，**无 `llm.chat`** → PASS。

### 6.3 B2 第二 / 三批核心证据：真实多轮主循环 + 工具执行链路

一次真机请求 `帮我查询一下明天呼吸内科有没有号源`（23.82s，真实 `qwen2.5:7b` + 本地 HIS）：

```text
events: agent_start, turn_start, text_response, tool_execute, tool_result,
        turn_end, turn_start, text_response, agent_end
spans:  context.assemble, llm.chat, tool.execute, llm.chat,
        memory.write, http.request, memory.load
```

* `turn_start` ×2、`llm.chat` ×2、`turn_end` ×1 → `LoopController` 多轮主循环 +
  `ModelRunner` 两个模型回合（第二轮无工具调用直接收尾，故不发 `turn_end`，与既有行为一致）；
* `tool_execute → tool_result` 严格有序；`tool.execute` span 属性
  `tool_name=查询是否有号源`、`success=True`、`execution_time_ms=2.21`、
  `result_preview=呼吸内科 在 2026-11-13 上午有号源，共 5 个号，出诊医生：蔡柏蔷。`
  → `ToolCoordinator` 的工具执行、结构化结果与 `messages` 回填链路完整；
* `llm.chat` span 属性 `model=qwen2.5:7b`、`turn=0`、`tool_call_count=1`、`prompt_tokens=2793`；
* `agent_start` / `agent_end` 各恰好一次，首尾顺序不变。

这条链路同时穿过本批两个新协作者：`run() → LoopController.run_loop() → _execute_tools 委托壳
→ ToolCoordinator.execute_tools()`，且事件序列与 B0 冻结的行为表一致。

### 6.4 收尾核查

| 项 | 结果 | 证据 |
|---|---|---|
| V3 全程日志干净 | PASS | 新增 23 行日志，`bad_markers=[]`（无 `GeneratorExit` / `RuntimeError` / `Traceback` / `Exception in ASGI`） |
| V4 资源无泄漏 | PASS | `inflight_requests=0`；`requests_total=8 == run_finished_total=8`；`errors_total=0` |

```text
=== SUMMARY ===
  R1_event_order: PASS                       V1_short_circuit_bypasses_runner: PASS
  R2_trace_ids: PASS                         V2_loop_and_tool_chain: PASS
  R3_client_disconnect: PASS                 V3_no_bad_log_markers: PASS
  R4_no_cross_talk: PASS                     V4_health_and_no_leaked_inflight: PASS
```

> R3 的 HTTP 客户端断开走 `CancelledError` 路径（`run_cancelled_total`），
> `GeneratorExit` / `aclose()` 路径由进程内单测覆盖 —— 两条路径均有验证，不把
> `run_generator_closed_total` 当作 HTTP 断开的硬预期。

收尾：8082 隔离实例已停止；8081 用户常驻服务 `/health` 返回 200、`inflight_requests=0`，全程未被触碰。

---

## 7. 明确未做的事与观察项

按方案 §6「只修改当前批次明确列出的范围；不顺手进行无关重构」，以下均**不在本批**：

1. **异常路径的 `AgentEndEvent` 与 `task_tracker.finish()` 终态化 / `RunFinalizer`** —— 现状是 `run()` 的 `except` 只记指标并原样抛异常，三个短路在 `task_tracker.begin()` 之前 `return` 却仍调 `finish()`。修复会改变对外事件序列并撞 B1 的 3 条护栏，必须作为独立批次。
2. 删除 `EnginePhases` mixin —— 待 B2 全部协作者抽完统一处理（见 §3.3）。
3. `engine.py` 两处历史 F401：`AgentEndEvent`（未使用导入）与 `_trim_to_sentence`（E3 刻意保留的兼容别名，**不能删**）。
4. 科室守卫对「请问医院门诊几点开始挂号？」的误判（B1 已记录的观察项），本批不复现、不修。
5. 真实模型一次完整生成约 118s，真机链路以「收到首条 `text_response` 即断开」覆盖 `llm.chat`；完整链路的行为等价由 914 条离线用例与 298 行逐字平移证明共同守护。

---

## 8. 停止点

本批次到此停止：B2 第二 / 三批已独立回归并合并到 `main`。
下一批应单独处理 `RunFinalizer` 的异常路径 `AgentEndEvent` / `task_tracker.finish()` 终态化，
并在 B2 收尾后统一删除 `EnginePhases` mixin；不要在协作者拆分批次里顺手修异常终态。

> **后续纠偏（2026-11-13，B3 决策确认）**：上段与 §7 第 1 条的「异常路径 `AgentEndEvent` 终态化」提法需收紧。
> `run()` 的 `finally` 不能 `yield`，异常 / `GeneratorExit` 路径**无法补发 `AgentEndEvent`**；
> 实际交付为「持久化终态 + 明确 outcome 指标」（**B3-3**），`RunFinalizer` 结构抽取为 **B3-4**，
> mixin 删除随 B3-4 一并处理。详见 `docs/主体Agent整改-B3-调研与分批实施方案.md` §4。
