# 主体 Agent 整改 · B2 第一批（ModelRunner）交付与验收

> **对应方案**：`docs/主体Agent生产化架构最终整改方案.md` §3.1 / §3.2 / §4 P1-1.1（迁移映射与「先抽协作者、再迁移方法、最后删 mixin」）、§4 P1-1.5（事件顺序兼容）、§6 批次 B2
> **前置基线**：B1 提交 `e94f459`（`docs/主体Agent整改-B1-交付与验收.md`，889 passed / 6 skipped）
> **批次主题**：从 `EnginePhases` mixin 抽出第一个显式协作者 `ModelRunner`
> **交付状态**：代码已完成；离线回归与真机验收**已在本环境实跑通过**（延续用户 2026-11-12「你本地自己测试」的要求，详见 §5）。
> **本批边界**：只抽 `_chat_turn` 一个方法。`_run_loop`、三个确定性短路、`_finalize`、异常路径终态化一律不动。

---

## 1. 基线对照

| 项 | B1（前置） | B2 第一批后 |
|---|---|---|
| 提交 | `e94f459` | 基于 `e94f459` 的本批提交 |
| 全量单测 | 889 passed / 6 skipped / 1 warning / 29.98s | **897 passed / 6 skipped / 1 warning / 29.45s** |
| 退出码 | 0 | 0 |
| skip 原因 | `test_live_api.py` 需 `--run-integration` | 不变 |
| warning | starlette `anyio.abc.BlockingPortal`（第三方） | 不变 |
| ruff `E9,F63,F7,F82,F811` | All checks passed | All checks passed |
| 新增用例 | 13 条（`test_run_context.py`） | **8 条**（`test_model_runner.py`）；889 + 8 = 897 |
| `EnginePhases` 行数 | 1,465 | **1,183**（净减 282 行） |

定点复跑：`pytest tests/unit/test_model_runner.py -q` → **8 passed in 0.59s**。

---

## 2. 变更文件与接口变化

### 2.1 文件清单

| 文件 | 变化 |
|---|---|
| `xiaozhi/runtime/model_runner.py` | **新增**。`ModelRunner` 协作者，承载单轮模型调用 |
| `xiaozhi/runtime/engine_phases.py` | `_chat_turn` 的 282 行实现改为 24 行委托壳；清理只被它使用的 6 组导入；模块 docstring 说明抽取结果 |
| `xiaozhi/runtime/engine.py` | `__init__` 末尾组装 `self._model_runner`，并注入两个判定型 Port；新增一处导入 |
| `tests/unit/test_model_runner.py` | **新增** 8 条护栏 |
| `docs/主体Agent整改-B2-第一批-ModelRunner交付与验收.md` | 本文 |

### 2.2 协作者接口

```python
class ModelRunner:
    def __init__(
        self,
        *,
        has_pending_approval_token: Callable[[], bool],
        output_guard_enabled: Callable[[], bool],
    ) -> None: ...

    async def chat_turn(
        self, *,
        chat_model, retry_config, metrics,
        messages, tools, known_tool_names, state, user_input,
        trace_id, turn_number, request_span, result, outcome,
    ) -> AsyncIterator[Event]: ...
```

`AgentEngine.__init__` 组装：

```python
self._model_runner = ModelRunner(
    has_pending_approval_token=self._has_pending_approval_token,
    output_guard_enabled=self._output_guard_enabled,
)
```

`EnginePhases._chat_turn` 保留为委托壳（调用点零改动）：

```python
async for _ev in self._model_runner.chat_turn(
    chat_model=self.chat_model,
    retry_config=self.retry_config,
    metrics=self.metrics,
    messages=messages, tools=tools, ...,
    result=result, outcome=outcome,
):
    yield _ev
```

### 2.3 逐字平移的程序化证明

用脚本从 `git show HEAD:.../engine_phases.py` 取出旧 `_chat_turn` 方法体，与本批 `ModelRunner.chat_turn` 方法体做归一化 diff：

```text
old body lines=264  new body lines=264
EQUIVALENT: 方法体逐字一致，仅有 5 处依赖标识符替换
  self._has_pending_approval_token() -> self.has_pending_approval_token()
  self._output_guard_enabled()      -> self.output_guard_enabled()
  self.chat_model                   -> chat_model
  self.retry_config                 -> retry_config
  self.metrics                      -> metrics
```

即：264 行实现（含全部注释、全部 `yield` 时机）没有任何语义编辑，只把 5 处「隐式从 mixin 的 `self` 取依赖」改成显式依赖。

---

## 3. 设计取舍

### 3.1 为什么第一批只抽 `ModelRunner`

方案 §6 把 B2 写成「抽取 `LoopController` / `ModelRunner` / `ToolCoordinator`，行为与事件顺序不变」，但 §4 P1-1.1 明确要求「先抽协作者、再迁移方法、最后删 mixin，不做一次性大重写」。三者的风险差异很大：

| 协作者 | 来源方法 | 本批不抽的原因 |
|---|---|---|
| **`ModelRunner`** | `_chat_turn`（380–661） | **本批抽取**：`_run_loop` 单点调用；不改 `messages`；事件只有 `TEXT_RESPONSE` / `DegradedEvent`；三个短路完全不经过它；输入输出天然是 DTO |
| `LoopController` | `_run_loop`（1192–1357） | 主干，控制全部 `TURN_START` / `TURN_END` / 降级 / 审批短路的事件顺序，且 `held_deltas` 补发与 `_finalize_response_gate` 交织，应后置 |
| `ToolCoordinator` | `_execute_tools`（663–828） | 跨文件依赖 `engine.py` 的 `_assistant_to_ollama` / `_tool_to_ollama`，且审批 `break` 会外溢到主循环，边界需单独设计 |

### 3.2 为什么运行时依赖按调用传入，而不是构造期注入

方案 §3.2 要求「runtime 之间通过明确的数据对象传递，不通过继承 mixin 访问其他模块的私有属性」。实现上有两种做法，本批选择了后者：

* 构造期注入 `chat_model` / `retry_config` / `metrics` 看似更「标准」，但仓库既有装配与 `tests/unit/test_approval_auto_execution.py` 有 10 处 **`engine.chat_model = FakeChatModel(...)` 构造后替换**。快照会让这些注入静默失效，属于行为变更。
* 因此：两个判定型 Port 在构造时注入（它们是请求级 contextvar / 配置，必须每轮现取）；三个运行时依赖在每次 `chat_turn()` 调用时显式传入。协作者不持有 `AgentEngine` 实例，也不访问其私有属性。

`tests/unit/test_model_runner.py` 用三条测试分别钉住这个契约：构造期不快照、构造后替换模型仍生效、判定 Port 每轮现取。

### 3.3 为什么保留委托壳

方案 §4 P1-1.1 的迁移顺序是「先抽协作者、再迁移方法、最后删 mixin」。本批不删 `EnginePhases._chat_turn`，让 `_run_loop` 与既有单测调用点零改动；待 `LoopController` 抽完后再统一删除 mixin。

---

## 4. 行为与事件顺序兼容性

新增**完整事件序列快照**断言（此前仓库只有首尾事件与 `any/in` 存在性断言，中间相对顺序无守护）：

| 场景 | 冻结的事件序列 |
|---|---|
| 正常问答 | `agent_start, turn_start, text_response, agent_end` |
| 问候短路 | `agent_start, text_response, agent_end` |
| 医疗安全短路 | `agent_start, text_response, agent_end` |
| 模型抛错降级 | `agent_start, turn_start, text_response, degraded, agent_end` |

另外钉住：

* `AgentEndEvent` 在正常、短路、模型错误三条路径各恰好一次；
* 问候与医疗安全短路 `chat_model.calls == 0`（完全不经过 `ModelRunner`）；
* 委托壳与协作者本体对同一输入的事件类型、metadata、`outcome` 全键值、`result.text` / `degraded`、模型调用次数逐字一致；
* B1 的 13 条护栏全部原样通过（生命周期壳、trace 恢复、取消 / `GeneratorExit` 收尾未受影响）。

---

## 5. 离线回归与真机验收（本环境实跑）

### 5.1 离线

```text
./.venv/bin/python -m pytest tests/ -q -ra
897 passed, 6 skipped, 1 warning in 29.45s / EXIT=0

./.venv/bin/python -m pytest tests/unit/test_model_runner.py -q -ra
8 passed in 0.59s / EXIT=0

./.venv/bin/python -m ruff check --select E9,F63,F7,F82,F811 xiaozhi/ tests/
All checks passed!
```

### 5.2 真机（8082 隔离实例，认证开启；8081 用户常驻进程不触碰）

先复跑 B1 四项回归，确认本批没有回退既有语义：

```text
V1_event_order: PASS          events=['agent_start','text_response','agent_end']
V2_trace_ids: PASS            run_id/request_id 在 http.request span，X-Trace-Id 与 /trace/{id} 对齐
V3_client_disconnect: PASS    cancelled+1 / finished+1 / bad_markers=[]
V4_no_cross_talk: PASS        serial_ok=True parallel_ok=True
```

B2 专属四项：

| 项 | 结果 | 证据 |
|---|---|---|
| B2-V1 短路绕过协作者 | PASS | 问候序列 `agent_start, text_response, agent_end`；trace spans = `greeting.fast_path / memory.write / http.request / memory.load`，**无 `llm.chat`** |
| B2-V2 真实模型回合 span 完整 | PASS | 事件前缀 `agent_start, retrieval, turn_start, text_response`；`llm.chat` span 属性 `model=qwen2.5:7b`、`turn=0` |
| B2-V3 客户端中断日志干净 | PASS | 新增 8 行日志，`bad_markers=[]`（无 `GeneratorExit` / `RuntimeError` / `Traceback` / `Exception in ASGI`） |
| B2-V4 资源无泄漏 | PASS | `inflight_requests=0`，`requests_total=8 == run_finished_total=8` |

> B2-V2 的 `llm.chat` span 带 `error: True`：验证脚本在收到首条 `text_response` 后主动断开流式请求，span 的 `__exit__` 按既有设计记录取消事实，属预期；服务端日志无任何异常堆栈。

收尾：8082 隔离实例已停止；8081 常驻服务 `/health` 返回 200、`inflight_requests=0`，未被触碰。

---

## 6. 明确未做的事与观察项

按方案 §6「只修改当前批次明确列出的范围；不顺手进行无关重构」，以下均**不在本批**：

1. `LoopController` / `ToolCoordinator` / `RunFinalizer` 抽取 —— B2 后续批次。
2. **异常路径的 `AgentEndEvent` 与 `task_tracker.finish()` 终态化** —— 现状是 `run()` 的 `except` 只记指标并原样抛异常，既不发 `AgentEndEvent` 也不 `finish()`；三个短路在 `task_tracker.begin()` 之前 `return` 却仍调 `finish()`（tracker 因无 `task_id` 静默 no-op）。本批先用特征测试钉住现状，不「顺手修好」，因为它会改变对外事件序列并撞 B1 的 3 条护栏。
3. `engine.py` 两处历史 F401：`AgentEndEvent`（未使用导入）与 `_trim_to_sentence`（E3 刻意保留的兼容别名）。后者不能删，前者不在本批范围。
4. 科室守卫对「请问医院门诊几点开始挂号？」的误判（B1 已记录的观察项），本批不复现、不修。
5. 真实模型一次完整生成约 118s，本批真机用「收到首条 `text_response` 即断开」的方式覆盖 `llm.chat` 链路；完整链路的行为等价由 897 条离线用例与 264 行逐字平移证明共同守护。

---

## 7. 停止点

本批次到此停止：B2 第一批已独立回归并合并到 `main`，不叠加 `LoopController` / `ToolCoordinator` 的结构性改动。下一批应先抽取 `LoopController`（含 `held_deltas` 补发、降级与 `TURN_END` 顺序的完整序列快照），再单独处理 `RunFinalizer` 的异常路径终态化。

> **后续纠偏（2026-11-13，B3 决策确认）**：本文 §6 第 2 条与上段「异常路径 `AgentEndEvent` 终态化」的提法需收紧。
> `run()` 的 `finally` 不能 `yield`，异常 / `GeneratorExit` 路径**无法补发 `AgentEndEvent`**；
> 实际交付为「持久化终态 + 明确 outcome 指标」（**B3-3**），`RunFinalizer` 结构抽取为 **B3-4**。
> 详见 `docs/主体Agent整改-B3-调研与分批实施方案.md` §4。
