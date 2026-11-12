# 主体 Agent 整改 · B3 调研与分批实施方案（阻塞 IO 与任务生命周期状态）

> **对应方案**：`docs/主体Agent生产化架构最终整改方案.md` §1.3（取消语义）、§1.4（阻塞 IO 治理）、§3.3（超时与结果状态模型）、§4 规则 8/9、§6 批次 B3
> **前置基线**：B2 已全部完成并合并到 `main`（`723e63c`，914 passed / 6 skipped，`docs/主体Agent整改-B2-第二三批-LoopController与ToolCoordinator交付与验收.md`）
> **批次目标**：async / bounded IO、写操作 `STARTED` 保证；停止条件「取消和故障用例通过」
> **本文性质**：原为调研与设计文档；**2026-11-13 用户已采纳 D1 / D2 / D3**（见 §4），本文升级为 **B3 执行计划**。批次按 §3 逐批独立回归、独立提交推送，不与下一批叠加。
> **状态**：§2 现状盘点保留 `723e63c` 时的快照不改；后续各批的实测结果写入各自交付文档。

---

## 1. 为什么 B3 不能按 B2 的「逐字平移」做法推进

B0→B2 的方法论是「抽协作者 + 委托壳 + 逐字等价证明」，因为那几批的硬约束是**行为与事件顺序不变**。
B3 不同：方案 §4 规则 8 要求「写操作执行前的 `STARTED` 生命周期记录必须持久化成功；若关键记录不可写，
则**拒绝进入外部副作用阶段**」，而当前实现恰好相反 —— 落库失败只记 warning，工具照常执行。
这是**真实的行为变更**（新增一条拒绝执行的路径），会改变事件序列与 `ToolResult`，
因此不能用等价性证明，必须用「特征测试钉住旧行为 → 改语义 → 故障注入用例」的节奏。

---

## 2. 现状盘点（基于 `723e63c`）

### 2.1 写操作 `STARTED` 链路

```text
ToolCoordinator.execute_tools()                         runtime/tool_coordinator.py:119-168
  ├─ tool = tool_registry.get(call.name)                :121
  ├─ tool_exec_id = task_tracker.tool_started(...)      :127-130   ← 同步调用
  ├─ tool_result = await secure_executor.execute(...)   :153       ← 可能已产生副作用
  └─ task_tracker.tool_finished(tool_exec_id, ...)      :165-168
```

`TaskTracker.tool_started()`（`runtime/task_tracker.py:106-129`）：

```python
try:
    self._repo.record_tool_started(record)
    return record.tool_execution_id
except Exception as exc:
    logger.warning("工具执行前落库失败（忽略）tool=%s: ...", ...)
    return None          # ← 失败被吞掉，调用方无从区分「没装配」与「落库失败」
```

模块 docstring 明确写「**追踪失败绝不打断对话**……任务持久化是可观测性，不是业务依赖」。
这与方案 §4 规则 8 直接冲突；`task_repo.py:8-13` 的崩溃恢复设计又依赖 `STARTED` 一定存在，
否则进程在副作用提交后崩溃时恢复扫描看不到任何记录，无法转成 `UNKNOWN` 待对账。

`begin()` / `finish()` / `tool_finished()` / `_event()` 同样是「捕获异常 → warning → 吞掉」。

### 2.2 同步阻塞 IO 点位

| 位置 | 现状 | 方案 §1.4 要求 |
|---|---|---|
| `repositories/task_repo.py:303-305` | `mysql.connector.connect(...)` 同步连接；`create_task` / `record_tool_started` 等全为同步方法，被 async 请求链路直接调用 | 有界线程池或异步客户端 |
| `memory/store.py:263` | `self._db().find_one(...)` 同步 pymongo 直接在 `async load()` 里调用；只有 `ping()`（`:252-259`）用了 `asyncio.to_thread` | 统一搬迁，避免「只有探活异步」 |
| `service/his_client.py:309` | `urllib.request` 同步 HTTP；重试退避用 `time.sleep(next_delay)`（`:378`） | 异步客户端 / 有界线程池；`sleep` 换 `await asyncio.sleep` |
| `profile/store.py` / `profile/vector.py` | 数据库 / Milvus 同步调用 | 同上 |
| `ops/audit.py:86-88` | `MongoClient` 同步 | 同上 |

已有的正确范例：`ops/lifecycle.py:60` 用 `asyncio.wait_for(asyncio.to_thread(probe), timeout=...)`，
`ops/redis_limiter.py:143` 用 `to_thread`。但都是**无界默认线程池**，没有专用、有界、可观测的执行器，
也没有统一的 connect/read/write timeout 或 shutdown 排空。

### 2.3 灰度开关（必须在设计里显式处理）

`task_repo.py:15-16`：`task_tracking_enabled=false`（默认）时组合根不装配仓储，
引擎 `task_tracker=None`，`ToolCoordinator` 的 `task_tracker is not None` 分支整体跳过。
因此 **B3 的语义变更默认不会触达现有部署**，但也不能因此把「灰度关闭」当成永久豁免：
规则 8 的适用范围需要明确决策（见 §4 的待决策点 D1）。

### 2.4 取消与超时语义

方案 §1.3 已明确：`asyncio` 超时只控制本地等待，不代表对端停止处理；
写工具超时必须保留 `STARTED` 并转 `UNKNOWN`（`task_repo.py:8-13` 的恢复扫描已实现"标记 UNKNOWN 并上报、不自动重试"）。
当前 `ToolResult` 只有 `success / content / error_type / metadata`，`TurnState.record_write_result()`
已读取 `metadata["authoritative_status"] == "UNKNOWN"`，`state.has_unknown_write` 也已有 ——
**状态容器已具备，缺的是执行链路在超时/取消时真的写入这个状态**。

---

## 3. 执行批次（2026-11-13 用户确认；每批独立回归、独立提交推送）

| 批次 | 工作项 | 行为是否变化 | 停止条件 |
|---|---|---|---|
| **B3-1** | 结构准备：新增有界、可观测的专用 IO 执行器（带队列上限、指标、shutdown 排空）；把 `memory.store` 的同步 Mongo 调用、`task_repo` 的同步 MySQL 调用、`his_client` 的同步 HTTP 统一经它调度；`time.sleep` 退避换 `await asyncio.sleep` | **不变**（只换执行位置，不改返回值/异常/事件） | 914 passed 基线不回退；真机 V1–V4 + B2 工具链路回归全 PASS |
| **B3-2** | 写操作 `STARTED` 强保证（D1/D2 落点）：收敛 `WRITE_TOOL_NAMES` 单一常量；新增显式策略 `require_started_for_writes`（默认 false）+ 组合根 WARNING + 启动期一致性校验；`tool_started` 返回 `DISABLED / RECORDED / FAILED` 三态；写工具在 `FAILED` 时拒绝进入 Adapter、返回结构化失败；只读放行但记 `task_started_failed_total` 与 WARNING | **变化**（新增拒绝路径） | §3.2 故障注入矩阵全通过；确认**没有任何**用例在 tracker 落库失败时仍执行了写工具；灰度关闭时事件序列不变 |
| **B3-3** | 取消 / 故障终态语义（D3 前半）：`run()` 的 `finally` 中幂等持久化 `task_tracker.finish()` + 明确 outcome 指标；超时 / 客户端断开 / 执行后审计失败落 `UNKNOWN` 或待核查；`recover_stale()` 的 UNKNOWN 收窄到写工具，消除只读 phantom UNKNOWN。**不补发 `AgentEndEvent`** | **变化**（补齐持久化终态） | 取消和故障用例通过（方案 §6 B3 停止条件）；B1 的 13 条护栏无需删改断言 |
| **B3-4** | 抽取 `RunFinalizer`（D3 后半）：语义定死后，把 `_finalize_response_gate` / `_persist_and_extract` / `_finalize` 平移到协作者，接口 `finalize(outcome, ...)` 一次到位；`EnginePhases` 保留委托壳 | 不变 | 复用 B2 逐字等价证明；全量回归 + 真机全 PASS |
| **B3-5** | 可选：把 `urllib` / `mysql.connector` / `pymongo` 替换为成熟异步客户端（连接池 + connect/read/write timeout + 关闭生命周期） | 不变（但风险高） | 独立批次，不与 B3-1~4 叠加；方案 §6 未列为必须，若 B3-1 的有界执行器已达标可不做 |

> **顺序调整说明**：原 §3 的 B3-4「可选异步客户端」顺延为 B3-5；`RunFinalizer` 从「B2 后续」正式归入 B3-4。理由见 §4 D3。

### 3.1 B3-1 的等价性判据

B3-1 仍属于「行为不变」批次，可复用 B2 的逐字证明思路，但对象从「方法体」换成「调用契约」：

* 同一输入下，经有界执行器前后的返回值、异常类型、`ToolResult` 字段、事件序列逐字一致；
* 线程池队列满时按既有超时/失败语义处理，**不得静默丢弃任务**；
* shutdown 时等待可安全收尾的后台任务，禁止遗留无主 `create_task`（方案 §1.4 策略 4）。

### 3.2 B3-2 的故障注入矩阵

| 注入点 | 期望（D1/D2 已定） |
|---|---|
| `record_tool_started` 抛异常（写工具） | **拒绝**：不调用 `secure_executor.execute`、不产生副作用；返回结构化失败（`error_type=TaskStartUnavailable`）；`ToolResultEvent.is_error=True`；`task_tracker.tool_finished` 不被调用；记 `task_started_failed_total` |
| `record_tool_started` 抛异常（只读查询） | **放行**（D2）：正常进入 Adapter；记 `task_started_failed_total{effect=read}` + 带 `trace_id` / `tool_call_id` 的 WARNING；`tool_finished` 保持 no-op |
| 未装配（`task_tracker=None`，DISABLED） | **放行**（D1=A）：写工具与只读工具事件序列均不变；组合根记 WARNING；`require_started_for_writes=True` 且 `tracker=None` 时启动期即失败 |
| 同一轮混合：一个只读 + 一个写工具，`STARTED` 落库失败 | 只读正常执行、写工具被拒、循环继续（**按单个 call 判定，不按整轮**）——必须有用例覆盖 |
| `record_tool_finished` 抛异常 | 副作用已提交，不得假装回滚；标记待核查并告警（方案 §4 规则 9）|

---

## 4. 决策点（已定 · 2026-11-13 用户采纳）

### D1｜灰度关闭时允许写操作进入 Adapter —— 选 A，但做成显式策略

**依据（已核准）**：`config.py:146` 默认 `task_tracking_enabled=False`，`.env` 未设置该键；`application.py:324-341` 仅在 true 时装配 `TaskTracker`；`routes.py:225-229` 仅在 true 时传幂等键；`tool_coordinator.py:127-130` 在 `task_tracker is None` 时整段跳过 STARTED。选 B 会让所有默认部署（含 8082 真机、未配 task 库的环境）的预约/取消立即不可用，并推翻 B0 冻结基线。

**落地约束**（不是留一个隐式 `None` 豁免）：

1. 新增 `require_started_for_writes: bool = False`，按 B2 惯例由委托壳**每次调用**传入 `ToolCoordinator`，不在构造期快照。
2. 组合根在 `task_tracking_enabled=false` 时打 WARNING：「写操作当前无 STARTED 保障，崩溃恢复无法对账」。
3. 启动期一致性校验：`require_started_for_writes=True` 且 `task_tracker is None` → **启动即失败**，不留到每个请求里拒绝。
4. 规则 8 的全局成立路径是「灰度全量开启 + 策略切 true」，不在 B3 改默认。
5. 配套：`TaskTracker.tool_started()` 必须把「未装配」与「落库失败」区分开，返回 `DISABLED / RECORDED / FAILED` 三态，否则拒绝路径无从判定。

### D2｜只读查询在 `STARTED` 落库失败时放行 —— 强保证只作用于写工具

方案 §4 规则 8 只约束「写操作」与「外部副作用」；拒绝只读查询属于无谓可用性损失。放行必须同时写死：

* 门控**按单个 call** 判定，不按整轮；混合轮用例必须覆盖。
* 写工具清单先收敛为单一来源：现状 `state.py:140` 与 `task_tracker.py:40` **重复定义** `_WRITE_TOOLS`，B3-2 抽成公共 `WRITE_TOOL_NAMES` 供 `TurnState` / `TaskTracker` / `ToolCoordinator` 共用；**暂不用 `ToolDefinition.effect` 门控**（那是 B6 契约，提前用会出现两个真相）。
* 失败不许静默：`task_started_failed_total`（带 `effect=read` 标签）+ 带 `trace_id` / `tool_call_id` 的 WARNING。
* 已知副作用：`recover_stale()`（MySQL `task_repo.py:554-563`、内存版 `:257-263`）把**所有** STARTED 无差别标 UNKNOWN；只读工具「STARTED 成功但进程崩溃」会留下 phantom UNKNOWN 噪音，B3-3 收窄到写工具。

### D3｜先 B3-3 定终态语义，再 B3-4 抽 RunFinalizer

**原方案给的理由「避免同时改语义与改结构」不成立**：`RunFinalizer` 三块职责全在正常路径（`engine_phases.py:915-943`），B3-3 改的是 `run()` 生命周期壳（`engine.py:186-228`），两者正交。真实理由：

1. 终态化是方案 §6 的 B3 停止条件「取消和故障用例通过」，结构抽取不是。
2. 先定死 outcome 集合（completed / cancelled / error / generator_closed / UNKNOWN / WAITING_APPROVAL），`RunFinalizer.finalize(outcome, ...)` 才能一次设计好；先抽必然返工。
3. `_finalize` 现在被 `_run_body()` 第 6 步与三个短路分支共 4 处调用，靠 `result.error` / `state.approval_required` 反推终态；应由持有权威 `outcome` 的 `run()` 先定语义。

### 一处必须纠正的旧表述

B1 / B2 交付文档曾写「异常路径也保证 `AgentEndEvent`，随 `RunFinalizer` 收口」——**物理上做不到**：`run()` 的 `finally` 不能 `yield`，否则 `aclose()` / `GeneratorExit` 会抛 `async generator ignored GeneratorExit`。B3-3 的目标是「异常路径保证**持久化终态 + 明确 outcome 指标**，不保证 `AgentEndEvent`」。相关文档已加后续纠偏注记；`tests/unit/test_run_context.py:251/263/278` 只断言指标与异常原样抛出，**并未**钉住「不发 AgentEndEvent」，因此 B3-3 无需删改 B1 护栏。

---

## 5. 风险与护栏

1. **B1 的 13 条护栏**（2026-11-13 核准纠偏）：`tests/unit/test_run_context.py:251/263/278` 实际只钉住
   「`run()` 的 `except` 只记指标并原样抛异常」「`CancelledError` 不被吞」「`aclose()` 时 `finally` 安全收尾」，
   **没有**断言异常路径不发 `AgentEndEvent`。B3-3 只在 `finally` 做同步的持久化终态与指标（不 `yield`、不补发事件），
   与这 3 条断言不冲突，无需删改测试；B0 行为基线正文是历史冻结快照，不改写。
2. **B2 的 17 条护栏**：`test_tool_coordinator.py` 的 `test_task_tracker_started_finished_bracket_execution`
   断言 STARTED/FINISHED 成对；B3-2 的拒绝路径会让「没有 FINISHED」成为合法场景，需新增分支用例而非改旧断言。
3. **真机链路**：`帮我查询一下明天呼吸内科有没有号源` 是已验证的、能真实穿过两轮主循环 +
   工具执行的话术（23–63s），B3 各批真机回归优先复用；预约类写操作需另找可安全重复的话术或用本地 HIS 沙箱故障注入。
4. **环境口径**：`.venv/bin/python -m pytest tests/ -q -ra`（当前基线 914 passed / 6 skipped）；
   8081 用户常驻进程不触碰，真机一律起 8082 隔离实例；本机 `curl` 连 `127.0.0.1` 被代理接管，
   必须用 `httpx.Client(..., trust_env=False)`。
5. **不顺手做的事**：科室守卫误判、`engine.py` 两处历史 F401、Context trust / Memory ID（B4/B8）、
   身份权限（B9）、ToolDefinition 契约（B6）—— 均不在 B3。

---

## 6. 执行状态

2026-11-13：D1 / D2 / D3 已由用户采纳，本文升级为执行计划，代码基线 `8fd284a`（914 passed / 6 skipped）。
按 §3 顺序执行，每批独立全量回归 + 8082 隔离实例真机验证 + 独立提交推送；
**未经独立回归不得把相邻两批叠加进同一个提交**。各批实测结果写入各自的交付文档。
