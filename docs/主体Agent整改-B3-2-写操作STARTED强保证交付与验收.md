# 主体 Agent 整改 · B3-2 交付与验收：写操作 STARTED 强保证

> **冻结基线**：`2addb84`（B3-1 有界 IO 执行器，926 passed / 6 skipped）
> **交付时间**：2026-11-13
> **文档性质**：Phase 3 / 批次 B3-2 交付物。**本批次引入真实行为变更**
>（新增「写操作被拒绝」路径），是《主体Agent生产化架构最终整改方案》§4 规则 8
> 「关键记录不可写，则拒绝进入外部副作用阶段」的落地，D1/D2 决策按
> 2026-11-13 用户确认执行。

---

## 1. 决策落地摘要

| 决策 | 结论 | 实现 |
|---|---|---|
| **D1** 灰度关闭（tracker=None）是否允许写操作 | **允许**（方案 A），但显式化、可切换 | `require_started_for_writes`（默认 false）+ 组合根 WARNING + 启动期一致性校验；`DISABLED` 分支放行且事件序列零变化 |
| **D2** 只读查询 STARTED 失败是否放行 | **放行**，强保证只作用于写工具 | 按单个 call 判定；记 `task_started_failed_total` + 带 trace/call 的 WARNING；`tool_finished` 保持 no-op |
| **D3** | （不属本批） | B3-3 终态语义 → B3-4 RunFinalizer |

## 2. 核心变更

### 2.1 写工具清单单一来源：`xiaozhi/runtime/write_tools.py`

```python
WRITE_TOOL_NAMES: frozenset = frozenset({"预约挂号", "取消预约挂号"})
```

`runtime/state.py` 与 `runtime/task_tracker.py` 各自维护的 `_WRITE_TOOLS` 收敛至此；
两处门控/入账/副作用判定共用一份真相。`ToolDefinition.effect` 声明式门控仍留给 B6。

### 2.2 `TaskTracker` 三态 API

```python
class ToolStartStatus(str, Enum):
    RECORDED = "RECORDED"   # STARTED 已持久化，可进入外部副作用阶段
    DISABLED = "DISABLED"   # 追踪未启用（未装配或本轮未登记）
    FAILED   = "FAILED"     # 落库失败：写操作必须被拒绝

@dataclass(frozen=True)
class ToolStartResult:
    status: ToolStartStatus
    tool_execution_id: Optional[str] = None
```

* **主 API** `try_tool_started()`：永不抛异常，失败语义由状态表达；
* **兼容入口** `tool_started()`：仅返回 id 或 `None`（Phase 5 既有用例与调用方零改动）；
  DISABLED 与 FAILED 在此都映射为 `None` —— 需要区分必须用三态入口。

### 2.3 门控点（两条执行路径同步落地）

`ToolCoordinator.execute_tools`（主路径）与 `EnginePhases` 审批兜底执行路径：

| STARTED 结果 | 工具 | 行为 |
|---|---|---|
| `RECORDED` | 任意 | 与旧版完全一致（STARTED → 执行 → FINISHED） |
| `DISABLED` | 任意 | 放行，事件序列零变化（D1=A） |
| `FAILED` | **写工具** | **拒绝**：不调 `secure_executor.execute`、不产生副作用；`ToolResult.fail(error_type="TaskStartUnavailable")` 且 `ToolResultEvent.is_error=True`；**不调用 `tool_finished`**（没有 STARTED 记录可收尾）；记 `task_started_failed_total` + `task_started_failed_write_total` + WARNING |
| `FAILED` | 只读 | 放行执行；记 `task_started_failed_total` + WARNING（含 trace_id / tool_call_id / effect=read）；`tool_finished(None, …)` 照旧 no-op |

被拒绝的写操作仍进入失败账本（`state.record_tool_failure`）并回传模型，
由模型向用户解释「任务追踪暂不可用，已阻止写操作」，`metrics.record_tool` 照常计入失败。

### 2.4 组合根策略开关：`require_started_for_writes`（默认 false）

```python
def validate_task_tracking_settings(settings) -> None:
    # require_started_for_writes=true 但 task_tracking_enabled=false
    #   → 启动即失败（强保证物理上无法生效，不留到请求期才发现）
    # 灰度关闭 → 允许启动 + WARNING：写操作当前无 STARTED 保障，崩溃恢复无法对账
```

该开关消费在**组合根**（启动期），而非每次调用传入 `ToolCoordinator`：
`DISABLED` 分支的放行是 D1=A 的固定语义，`FAILED` 分支的拒绝与该开关无关；
启动期校验已保证「策略开启 ⇒ tracker 必然装配」，运行期无需重复判定。

### 2.5 新增指标

* `task_started_failed_total`：STARTED 落库失败总次数（含只读放行）；
* `task_started_failed_write_total`：其中**写工具被拒**次数（规则 8 生效次数）。

## 3. 验收：故障注入矩阵（§3.2）→ `tests/unit/test_task_start_guarantee.py`（11 条）

| 矩阵行 | 用例 | 结果 |
|---|---|---|
| FAILED + 写工具 → 拒绝 | `test_write_tool_is_rejected_when_start_fails`：无副作用、`TaskStartUnavailable`、`is_error=True`、`tool_finished` 未被调用、双计数器 +1 | PASS |
| FAILED + 只读 → 放行 | `test_read_only_tool_passes_when_start_fails`：执行器被调、仅记总量、`tool_finished` no-op | PASS |
| DISABLED → 放行 | `test_disabled_tracker_keeps_event_sequence_unchanged`：写/只读事件序列均 `["tool_execute","tool_result"]` 不变 | PASS |
| 混合轮按单个 call 判定 | `test_mixed_round_rejects_only_the_write_tool`：只读执行、写被拒、循环继续、failed=2/write=1 | PASS |
| `record_tool_finished` 抛异常 | `test_tool_finished_failure_does_not_mask_tool_result`（真实 TaskTracker + 收尾即抛仓储）：副作用已提交、结果照常返回 | PASS |
| 三态可区分 + 兼容入口 | `test_try_tool_started_three_states`：RECORDED / DISABLED / FAILED；`tool_started` 旧契约不变 | PASS |
| 启动期校验四象限 | `test_require_started_for_writes_startup_validation`（参数化 4 例）：仅 `(tracking=false, require=true)` 启动失败 | PASS |
| 清单单一来源 | `test_write_tool_names_single_source`：state 入账范围与 tracker 副作用判定同源 | PASS |

另有 `ToolCoordinator` 既有 9 条护栏全部保持（`RecordingTracker` 桩同步改用三态入口）。

```text
11 passed（新）+ 9 passed（既有）→ 全量 937 passed / 6 skipped / 1 warning / 41.99s / EXIT=0
```

## 4. 真机验收（双实例）

### PART A｜8082（默认灰度关闭）——「默认部署行为零变化」

| 项 | 内容 | 结果 |
|---|---|---|
| A1 | 启动日志含「有界 IO 执行器已装配」+「写操作当前无 STARTED 保障」WARNING，无启动失败 | **PASS** |
| A2 | B1/B2 回归（R1–R4 / V1 / V3 / V4 全 PASS；V2 为已知老会话模型波动，见 B3-1 文档） | PASS* |
| A3 | 全新 session 工具链路事件序列与 B2 冻结快照**逐字一致**（新门控零触达） | **PASS** |
| A4 | `task_started_failed_total = 0`（DISABLED 分支无任何门控副作用） | **PASS** |

### PART B｜8083（`TASK_TRACKING_ENABLED=true` + `REQUIRE_STARTED_FOR_WRITES=true`）

| 项 | 内容 | 结果 |
|---|---|---|
| B1 | 「任务生命周期持久化已启用」，无「无 STARTED 保障」WARNING，启动未失败 | **PASS** |
| B2 | 工具链路事件序列与冻结快照逐字一致（RECORDED 路径零行为差异） | **PASS** |
| B3 | MySQL 真实落库：本轮 `tool_executions` 恰一条 —— 查询是否有号源 `STARTED→SUCCEEDED`、`ended_at` 非空、`side_effect_committed=0`；`agent_executions` SUCCEEDED，task/execution 链完整 | **PASS** |

> B3 首查 FAIL 是断言过严（把表里**历史真机测试**遗留的 FAILED 记录也算进来了），
> 按 15 分钟时间窗口重查后确认本轮记录全部正确。

原始输出：`/tmp/b3_2_verify_8082_output.log`、`/tmp/b3_2_verify_8083_output.log`、
`/tmp/b3_2_verify_8082_result.json`、`/tmp/b3_2_verify_8083_result.json`。

## 5. 明确不做 / 遗留

1. **真实写工具（预约挂号）的端到端拒绝演练**：拒绝路径已由故障注入单测全覆盖；
   真机演练需要完整审批 UI 闭环 + 人为打挂 MySQL，留到 B3-3 的故障演练一起做。
2. **写工具清单的声明式化**：等 B6 `ToolDefinition.effect` 契约落地后从注册表派生。
3. **取消/故障路径的终态化（UNKNOWN、`finish()` 收口）**：属 B3-3。
4. `recover_stale()` 对只读工具的 phantom UNKNOWN：属 B3-3（收窄到写工具）。

## 6. 下一批（B3-3）入口

* `run()` 生命周期壳 `finally` 中幂等持久化 `task_tracker.finish()` + 明确 outcome 指标
  （completed / cancelled / error / generator_closed）；
* 超时 / 客户端断开 / 执行后审计失败 → `UNKNOWN` 或待核查标记；
* `recover_stale()` 的 UNKNOWN 收窄到写工具；
* **不补发 `AgentEndEvent`**（`finally` 不能 yield，B1 纠偏结论）。
