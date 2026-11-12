# 主体 Agent 整改 · B3 调研与分批实施方案（阻塞 IO 与任务生命周期状态）

> **对应方案**：`docs/主体Agent生产化架构最终整改方案.md` §1.3（取消语义）、§1.4（阻塞 IO 治理）、§3.3（超时与结果状态模型）、§4 规则 8/9、§6 批次 B3
> **前置基线**：B2 已全部完成并合并到 `main`（`723e63c`，914 passed / 6 skipped，`docs/主体Agent整改-B2-第二三批-LoopController与ToolCoordinator交付与验收.md`）
> **批次目标**：async / bounded IO、写操作 `STARTED` 保证；停止条件「取消和故障用例通过」
> **本文性质**：**调研与设计文档，不改任何生产行为**。B3 涉及写操作语义变更，必须先冻结现状、拆小批次，再逐批独立回归。

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

## 3. 建议拆成四个小批次（每批独立回归）

| 批次 | 工作项 | 行为是否变化 | 停止条件 |
|---|---|---|---|
| **B3-1** | 结构准备：新增有界、可观测的专用 IO 执行器（带队列上限、指标、shutdown 排空）；把 `memory.store` 的同步 Mongo 调用、`task_repo` 的同步 MySQL 调用、`his_client` 的同步 HTTP 统一经它调度；`time.sleep` 退避换 `await asyncio.sleep` | **不变**（只换执行位置，不改返回值/异常/事件） | 914 passed 基线不回退；真机 V1–V4 + B2 工具链路回归全 PASS |
| **B3-2** | 写操作 `STARTED` 强保证：`tool_started` 区分「未装配 / 落库失败」；写工具在关键记录不可写时拒绝进入 Adapter，返回结构化失败（不产生副作用），并发 `ToolResultEvent` | **变化**（新增拒绝路径） | 故障注入用例通过；确认**没有任何**测试用例在 tracker 落库失败时仍执行了写工具 |
| **B3-3** | 取消 / 故障语义：超时、客户端断开、执行后审计失败分别落到 `UNKNOWN` / 待核查；`task_tracker.finish()` 在异常与取消路径终态化 | **变化**（补齐终态） | 取消和故障用例通过（§6 B3 停止条件）；B1 的 13 条护栏若与终态语义冲突，需先更新基线文档再改断言 |
| **B3-4** | 可选：把 `urllib` / `mysql.connector` / `pymongo` 替换为成熟异步客户端（连接池 + connect/read/write timeout + 关闭生命周期） | 不变（但风险高） | 独立批次，不与 B3-1~3 叠加；方案 §6 未列为必须，若 B3-1 的有界执行器已达标可不做 |

### 3.1 B3-1 的等价性判据

B3-1 仍属于「行为不变」批次，可复用 B2 的逐字证明思路，但对象从「方法体」换成「调用契约」：

* 同一输入下，经有界执行器前后的返回值、异常类型、`ToolResult` 字段、事件序列逐字一致；
* 线程池队列满时按既有超时/失败语义处理，**不得静默丢弃任务**；
* shutdown 时等待可安全收尾的后台任务，禁止遗留无主 `create_task`（方案 §1.4 策略 4）。

### 3.2 B3-2 的故障注入矩阵

| 注入点 | 期望 |
|---|---|
| `record_tool_started` 抛异常（写工具） | 不调用 `secure_executor.execute`；返回结构化失败；`ToolResultEvent.is_error=True`；`task_tracker.tool_finished` 不被调用 |
| `record_tool_started` 抛异常（只读查询） | 按 D2 决策：可放行或同样拒绝，但必须有一致且可测的规则 |
| `record_tool_started` 返回 `None`（未装配） | 按 D1 决策；灰度关闭时须保持现有事件序列不变 |
| `record_tool_finished` 抛异常 | 副作用已提交，不得假装回滚；标记待核查并告警（方案 §4 规则 9） |

---

## 4. 待决策点（不替用户拍板，进入 B3-2 前必须定）

* **D1｜灰度关闭（`task_tracker=None`）时是否允许写操作进入 Adapter？**
  * A：保持现状（允许）。优点：默认部署行为零变更、不撞既有 914 条用例；缺点：规则 8 只在开启灰度后生效，默认仍无 `STARTED` 保障。
  * B：强制装配（拒绝）。优点：规则 8 全局成立；缺点：改变默认行为，需更新 B0 基线、可能影响本地 HIS 沙箱与真机预约链路。
  * 倾向：**先 A，把 B 作为灰度全量开启后的独立开关**，但需用户确认。
* **D2｜只读查询在 `STARTED` 落库失败时是否放行？** 方案规则 8 只约束「写操作」，倾向只读放行（不产生副作用），但要在文档与用例里写死。
* **D3｜B3-3 与 `RunFinalizer` 的先后**：`RunFinalizer`（§1.1 目标清单，负责 Response Gate / 持久化 / 任务终态 / 结束事件）与 `task_tracker.finish()` 终态化职责重叠。建议**先 B3-3 定语义、再抽 `RunFinalizer` 做结构平移**，避免同时改语义与改结构。

> 注：B2 交付文档 §8 把「异常路径 `AgentEndEvent` / `task_tracker.finish()` 终态化」列为下一批，
> 与方案 §6 的 B3 是**同一件事的两个侧面**：本文档把它归入 B3-3，并以方案 §6 的批次顺序为准。

---

## 5. 风险与护栏

1. **B1 的 13 条护栏**：`tests/unit/test_run_context.py` 钉住了 `run()` 的 `except` 只记指标并原样抛异常、
   异常路径不发 `AgentEndEvent`。B3-3 若补发终态事件，必须先更新 B0 行为基线文档，再改断言，
   不允许直接删测试。
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

## 6. 当前停止点

B3 **调研与分批设计完成，未改任何代码**。工作区代码基线仍是 `723e63c`（914 passed）。
下一动作应先确认 §4 的 D1 / D2 / D3，再启动 **B3-1（行为不变的结构准备）**；
未经独立回归不得把 B3-1 与 B3-2 的语义变更叠加进同一个提交。
