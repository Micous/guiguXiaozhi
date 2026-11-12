# 主体 Agent 生产化架构最终整改方案

> 项目：`Micous/guiguXiaozhi`  
> 评审基线：远程 `main`，审计基线提交 `ab9b8f3816175d62c0a70d67c756907c97a2e09f`  
> 方案日期：2026-11-12  
> 文档性质：架构整改与分阶段实施方案，不代表代码已按本方案修改或通过验收。

---

## 1. 最终结论

当前项目已经具备自研 Agent Harness 的基本形态：`AgentEngine` 负责请求编排，`TurnState` 集中跨轮状态，`ContextAssembler` 负责上下文预算，`ConversationStore` 负责会话存储，`ToolRegistry` 与 `SecureToolExecutor` 负责工具调用，审批、任务跟踪、医疗安全、审计、指标和 HIS 适配也已形成独立模块。

**不建议推倒重写，也不建议直接引入完整多 Agent 框架替换当前 Harness。** 当前更适合的路线是：保持模块化单体，按清晰的端口契约逐步收敛职责；先保证 Loop、Memory、Tools、Context 的端到端正确性和可恢复性，再进行身份与权限体系的完整生产化升级。

本次整改目标不是单纯减少文件行数，而是保证：

1. 每个模块只拥有一类明确职责，并且只有一个事实来源。
2. 模块之间通过稳定 DTO / Protocol / Port 交互，不通过继承、共享可变对象或隐式 `self` 传递职责。
3. 一个请求从输入、上下文、模型、工具、审批、业务副作用、持久化到最终响应，状态可追踪、结果可解释、故障可恢复。
4. 读操作失败可以有边界地降级；写操作出现不确定结果时必须进入 `UNKNOWN` / 对账，不能盲目重试。
5. 每个整改批次均可独立本地回归；未经回归的批次不叠加下一批结构性改动。

### 1.1 优先级调整（按本次确认执行）

| 顺序 | 模块 | 当前优先级 | 本阶段目标 |
|---|---|---:|---|
| 1 | Loop / 执行生命周期 | P0 | 收敛执行边界、预算、取消、重试、收尾 |
| 2 | Memory / 持久化 | P0 | 消息身份、顺序、并发、可靠保存与删除 |
| 3 | Tools / 工具执行 | P0 | 工具契约、超时、重试、审批、UNKNOWN |
| 4 | Context / 上下文 | P1 | 可信边界、摘要、优先级、Token 预算 |
| 5 | Identity / Permissions | P1（后置实施） | 完整认证、主体映射、权限体系生产化 |
| 6 | 真实部署与 HIS 联调 | 上线门禁 | 真实环境验证，不用本地替身代替 |

**重要口径：身份和权限的完整体系改造放到第五顺位，不等于允许绕过现有安全控制。** 工具阶段仍须维持最小权限检查和 fail-closed；生产环境继续禁止匿名身份、模拟 HIS 或本地 HIS 替身承担真实患者写操作。第五阶段完成前，不得把系统认定为真实患者生产就绪。

---

## 2. 当前架构判断与保留项

### 2.1 应保留的设计

- `runtime/engine.py` 的薄编排入口方向正确。其 `run()` 已按读历史、组上下文、初始化状态、执行 loop、收尾、落库和结束事件组织流程。
- `runtime/state.py` 的 `TurnState` 是跨轮状态单一事实来源的正确方向。后续应继续通过状态方法维护不变量，而不是重新散落闭包变量。
- `runtime/persister.py` 将消息组装与存储 IO 分开，职责边界应保留。
- `memory/store.py` 把 Mongo、文件和内存实现收敛到 `ConversationStore` 抽象，存储适配器方向正确。
- 工具审批使用持久化审批记录、参数快照和一次性 token；已有写操作审批闭环应保留。
- `TaskTracker` 对工具执行前的 `STARTED`、执行后的完成态和恢复识别有价值，但写操作的关键生命周期记录不能继续作为“失败只记日志”的普通可观测性。
- `APP_ENV=production` 的 fail-closed 配置方向应保留；`IMPLEMENTED_REAL_SLOT_ADAPTERS` 为空时，不得通过配置伪装成真实 HIS 已接通。

### 2.2 当前主要结构问题

| 区域 | 现状 | 整改方向 |
|---|---|---|
| Loop | `engine_phases.py` 以 `EnginePhases` mixin 承载约 1,465 行逻辑，靠共享 `self` 获取依赖 | 拆成显式协作者，由 `AgentEngine` 组装和编排 |
| Memory | 存储层有 CAS / 进程内锁，但消息内容签名去重会混淆“内容相同”和“同一条消息” | 引入不可变消息 ID、Turn ID、版本序号 |
| Tools | ToolDefinition 已有 timeout、retry、result schema、required scopes 等契约字段，但执行链路并未全部兑现 | 统一在 ToolCoordinator / SecureToolExecutor 中执行契约 |
| Context | 文档优先级与实际预算分配不一致；历史摘要被包装成 System 消息 | 明确指令层级与不可信资料边界 |
| 生命周期 | 同步存储 / HIS IO 可能进入 async 请求链路；任务状态收尾和请求取消需要统一治理 | 贯穿式 Deadline、Cancellation、执行状态与资源清理 |
| 依赖关系 | 部分模块通过 mixin、共享状态或 service 内部调用形成隐式依赖 | 以 Protocol / Port 隔离业务核心与基础设施 |

---

## 3. 目标架构

采用**模块化单体 + 自研 Agent Harness + Ports & Adapters**。当前不拆微服务，不引入没有明确收益的分布式组件；先让模块边界和运行语义稳定。

```text
HTTP / WebSocket / API
        |
        v
Request Facade / Application Composer
        |
        v
AgentEngine（薄编排器）
        |
        +--> LoopController
        |       +--> ModelRunner
        |       +--> ToolCoordinator
        |       +--> ExecutionBudget / CancellationScope
        |
        +--> ContextService
        |       +--> HistoryLoader
        |       +--> RetrievalPort
        |       +--> ProfileContextPort
        |
        +--> MemoryService
        |       +--> ConversationStore
        |       +--> MessageIdentity / TurnSequencer
        |       +--> Persister
        |
        +--> Policy / ResponseGate / MedicalGuard
        |
        +--> TaskLifecycle / Trace / Audit
                |
                v
       Ports（业务侧接口与稳定 DTO）
                |
                v
       Adapters（Mongo / MySQL / Milvus / Redis /
                 Ollama / HIS / 文件系统）
```

### 3.1 模块职责边界

| 模块 | 唯一职责 | 不应承担的职责 |
|---|---|---|
| `AgentEngine` | 接收请求、创建 RunContext、按固定顺序调用协作者、汇总结果 | 具体模型协议、工具参数校验、数据库读写细节 |
| `LoopController` | 控制模型轮次、最大轮数、预算、重试决策、结束条件 | 组装 Prompt、直接访问业务 Repository |
| `ModelRunner` | 调用模型、归一化模型响应、流式输出、模型错误分类 | 执行工具或判断业务副作用是否成功 |
| `ToolCoordinator` | 工具调用编排、工具次数预算、执行状态推进、结果回填 | 直接实现预约/HIS 业务规则 |
| `SecureToolExecutor` | 按策略执行授权、审批核验、入参校验、注入防护和审计 | 负责模型重试或会话历史管理 |
| `ContextService` | 决定本轮上下文资料、可信标签、预算分配和裁剪 | 修改会话持久化记录 |
| `MemoryService` | 消息身份、Turn 顺序、会话保存与恢复 | 决定医疗业务回答或工具权限 |
| `Persister` | 生成合法的消息序列与元数据 | 执行 Mongo / MySQL IO |
| `ProfileService` | 画像抽取、查询、遗忘与删除状态 | 从用户自由文本自行确定患者身份 |
| `TaskLifecycle` | 记录 Task / Execution / ToolExecution 状态，支持 UNKNOWN 恢复 | 把关键写操作状态记录失败当作普通日志失败 |
| `Application Composer` | 创建并注入各模块、选择基础设施 Adapter | 承载业务逻辑 |

### 3.2 依赖规则

1. 依赖方向只能是 `Application → Domain/Ports → Adapters`。
2. 业务模块不能直接 import 具体 Mongo、Milvus、MySQL、HTTP 客户端实现。
3. `runtime` 之间通过明确的数据对象传递，不通过继承 mixin 访问其他模块的私有属性。
4. `TurnState` 是一次 Run 内的状态；`RunContext` 是一次请求的依赖与运行信息；二者不能混成一个无限增长的全局上下文对象。
5. 所有异步 IO 必须有明确的 timeout / cancellation / error semantics；同步阻塞 IO 必须迁移为异步客户端或有界线程池执行。
6. 不为“看上去解耦”提前拆微服务，不新增无数据支持的缓存、并行化和复杂调度设施。

---

## 4. 分阶段实施方案

## Phase 0：冻结行为基线与验收合同

**目标：先把现有行为钉住，避免结构拆分改变已经修复的业务语义。**

涉及文件：

- `guiguXiaozhi_backend_py/xiaozhi/runtime/engine.py`
- `guiguXiaozhi_backend_py/xiaozhi/runtime/engine_phases.py`
- `guiguXiaozhi_backend_py/xiaozhi/runtime/state.py`
- `guiguXiaozhi_backend_py/xiaozhi/runtime/persister.py`
- `guiguXiaozhi_backend_py/xiaozhi/memory/context.py`
- `guiguXiaozhi_backend_py/xiaozhi/memory/store.py`
- `guiguXiaozhi_backend_py/xiaozhi/security/secure_executor.py`
- `docs/python-production-baseline.md`

工作内容：

1. 锁定当前远程 `main` 的提交 SHA 和现有测试基线；记录已有通过数、跳过数和跳过原因。
2. 对正常问答、连续工具调用、查询类工具、写工具审批、审批后参数快照执行、模型纠正重试、超时、存储失败、客户端断开、UNKNOWN 对账等流程建立行为表。
3. 记录以下不可回退语义：
   - 模型没有真实执行写工具，不得输出“预约成功/取消成功”；
   - 写工具必须先经过参数校验、授权和审批策略；
   - 审批后执行用户确认的 canonical args，不能执行模型第二轮改写后的参数；
   - 写工具超时或连接中断不能直接认定失败，更不能无条件重试；
   - 工具调用消息与 tool result 必须按 `tool_call_id` 配对保存；
   - 医疗安全短路优先于问候、检索和模型调用；
   - Prompt 历史、RAG 和工具结果不得覆盖系统级安全指令。
4. 为后续结构拆分定义统一的端到端 Trace 字段：`request_id / trace_id / run_id / turn_id / message_id / tool_call_id / tool_execution_id / approval_id / business_idempotency_key`。已有字段可兼容映射，不要求一次性全部迁移。

**验收：** Phase 0 不要求改变生产行为；必须得到可重复执行的基线测试清单和端到端状态图。后续每个 Phase 都使用同一清单做回归。

---

## Phase 1：Loop 与执行生命周期收敛（第一优先级）

**目标：Agent 的一次 Run 有单一生命周期所有者；轮次、预算、取消、重试、收尾有明确边界。**

### 1.1 目标拆分

将 `EnginePhases` mixin 逐步拆为以下显式协作者：

| 新模块 / 类 | 从现有逻辑迁移的职责 | 主要来源 |
|---|---|---|
| `LoopController` | 主循环、最大轮数、结束条件、重试调度 | `engine_phases.py::_run_loop` |
| `ModelRunner` | 模型调用、流式输出、响应归一化、模型异常分类 | `engine_phases.py::_chat_turn` |
| `ToolCoordinator` | 工具调用序列、调用次数预算、结果回填 | `engine_phases.py::_execute_tools` |
| `ContextService` | 上下文准备和检索结果装配 | `engine_phases.py::_prepare_messages`，实际在 Phase 4 完成语义整改 |
| `ApprovalFlowCoordinator` | 审批后确定性执行及审批执行轮 | `engine_phases.py::_execute_approved_operation` |
| `RunFinalizer` | Response Gate、持久化调用、任务终态与结束事件 | `_finalize_response_gate / _persist_and_extract / _finalize` |
| `ExecutionBudget` | Run deadline、模型/工具/轮次预算 | 当前 `_time_budget_exceeded`、`max_tool_calls` 及相关配置 |

`AgentEngine` 只保留依赖注入、RunContext 创建和协作者顺序编排。拆分采用“先抽取协作者、再迁移方法、最后删除 mixin”的方式，不做一次性大重写。

### 1.2 统一运行上下文与预算

新增明确的请求级对象（名称可以按代码风格调整）：

```python
@dataclass(frozen=True)
class RunContext:
    run_id: str
    trace_id: str
    request_id: str
    session_id: str
    subject_id: str
    deadline_monotonic: float
    cancellation: "CancellationScope"
```

约束：

- `deadline_monotonic` 由入口创建一次，后续检索、模型、工具和持久化都读取剩余预算，不能每个阶段重新开始计时。
- 模型轮数、工具调用数、纠正重试数、每个工具失败次数由明确的 owner 管理，避免多处重复计数。
- `TurnState` 继续只承载本轮跨阶段业务状态；预算和取消状态不得散落进多个闭包。
- 超过预算时输出确定的 degraded / timeout 结果，并保证 `AgentEndEvent`、任务状态和 Trace 收口。
- 超时错误应区分 `TIMEOUT_BEFORE_EXECUTION`、`TIMEOUT_OUTCOME_UNKNOWN`，不能把所有 timeout 都转换成普通失败。

### 1.3 取消与收尾

在 `AgentEngine.run()` 建立清晰的 `try / except / finally` 生命周期：

- 客户端断开、请求任务取消、模型失败和内部异常分别有收尾语义。
- ContextVar、审批请求上下文、Trace span、任务 tracker 状态在 `finally` 中可靠释放或终态化。
- 取消只表示调用方不再等待，不表示远端写操作已经取消。
- 只读模型 / 检索 / 查询可以在安全边界内取消；写工具一旦可能已提交，必须保留 `STARTED` 并转为 `UNKNOWN` 待对账。
- 不能简单使用 `asyncio.wait_for()` 包住写工具并在 timeout 后认定“操作已取消”；底层 HTTP / HIS 可能已经完成副作用。

### 1.4 阻塞 IO 治理

重点核对：

- `memory/store.py` 中 Mongo 同步客户端调用；
- `repositories/task_repo.py` 对 MySQL 同步调用；
- `profile/store.py` 与 `profile/vector.py` 的数据库 / Milvus 调用；
- `service/his_client.py` 中同步 HTTP 请求；
- `TaskTracker` 在 async 请求生命周期中调用同步 Repository 的路径。

策略：

1. 优先选用成熟异步客户端并明确连接池、超时与关闭生命周期。
2. 短期无法替换的同步 IO 放入有界、可观测的专用线程池，禁止无界 `asyncio.to_thread` 泛滥。
3. 每种 Adapter 都有 connect/read/write timeout；写操作超时统一按不确定结果处理。
4. 服务 shutdown 时等待可安全收尾的后台任务，禁止遗留无主 `create_task`。

### 1.5 修改文件

- `runtime/engine.py`：缩小为薄编排层；统一 RunContext 与生命周期。
- `runtime/engine_phases.py`：分批迁移方法，最终移除 mixin；迁移前后保持对外事件顺序兼容。
- `runtime/state.py`：只保留业务状态账本，明确状态写入方法和不变量。
- `runtime/engine_helpers.py`：保留纯函数，不承接运行时状态。
- `runtime/task_tracker.py`、`repositories/task_repo.py`：将关键生命周期状态与普通可观测性区分；写操作执行前必须能可靠登记。
- `application.py`：在组合根注入新的协作者与 IO Adapter。

### 1.6 验收用例

- 模型成功、模型空响应、模型异常、纠正重试、已输出文本后发生模型异常：不能重复输出或拼接错误。
- 达到最大轮数、工具预算、总时间预算：结束状态明确，`AgentEndEvent` 不丢失。
- 客户端在模型流式输出中断开：连接资源、Trace、ContextVar 和任务状态正确收尾。
- 客户端在写工具调用期间断开：若结果未知必须 `UNKNOWN`，不自动重发。
- 连续多个请求和并发请求之间不存在 TurnState / 审批上下文 / trace 串扰。
- async 请求压力下，慢速 Mongo / HIS / Milvus 不得无界阻塞事件循环。

**Phase 1 完成标准：** Loop 不再依赖 mixin 隐式共享 `self`；一个 Run 的预算、取消、重试、终态只有一个明确责任方；现有业务行为测试全部通过。

---

## Phase 2：Memory 与持久化可靠性（第二优先级）

**目标：记忆系统不丢消息、不错误去重、不乱序；画像属于经过服务端绑定的主体；删除结果可追踪。**

### 2.1 消息身份与顺序

当前 `memory/store.py` 的 `_message_signature()` / `_merge_messages()` 以消息内容签名参与合并。内容相同不代表同一条消息，例如用户连续两次输入“你好”是两条合法消息，不应被去重。

建议消息持久化模型增加：

```python
message_id: str       # 每条消息不可变、全局唯一
run_id: str
turn_id: str
sequence: int         # 同一会话中的单调序号
created_at: datetime
role: str
tool_call_id: str | None
```

规则：

- 只按 `message_id` 做幂等去重；禁止按文本内容、角色和时间窗口推断消息相同。
- `tool_call_id` 配对校验必须保留；一次 tool call 和其 tool result 不能被单独裁剪或重排。
- 每次 Run 生成新的 `turn_id`；同一个 session 的 Turn 写入要有明确排序策略。
- 多实例部署不能依赖 `asyncio.Lock` 保证跨进程顺序。使用数据库 revision/CAS 或具有租约与 fencing token 的会话协调器；优先用数据库原子版本控制，避免先引入复杂分布式锁。
- CAS 冲突时只合并带唯一 ID 的新增消息，不能用文本签名“猜测”是否已写入。

### 2.2 持久化职责收敛

- `Persister` 只组装经过校验的消息序列。
- `ConversationStore` 只保存、读取、清理会话数据并提供明确版本冲突语义。
- `MemoryService` 负责 Turn 顺序、消息身份与存储协调。
- 读取历史必须返回明确版本 / revision；保存结果需能确认本次消息是否已经持久化。
- 保存失败的 degraded 结果要明确区分“模型已回答但记忆未保存”和“业务写操作结果未知”，两者不能混为一类。
- 不把画像抽取的普通失败与核心对话持久化失败混成一个错误等级。

### 2.3 画像抽取、注入和遗忘

相关文件：

- `profile/injector.py`
- `profile/service.py`
- `profile/store.py`
- `profile/vector.py`
- `runtime/engine_phases.py::_persist_and_extract`
- `repositories/session_repo.py`（按实际接口映射）

整改：

1. 画像抽取不得作为无主 `create_task` 直接 fire-and-forget。改为受控后台任务管理器，具备并发上限、超时、任务异常记录、shutdown 收尾；若业务要求跨进程可靠执行，再升级成持久化队列 / Outbox。
2. **画像身份不能从用户输入或历史中提取身份证号来决定检索对象。** 由服务端已验证的 session / principal 与患者关联关系提供 profile key。身份系统全面改造虽排在 Phase 5，但画像检索在此之前必须采用安全的 fail-closed 行为：没有服务端绑定 key 就不注入画像。
3. `user_key` 使用服务端密钥 HMAC 派生，密钥轮换策略明确；禁止裸身份证号或裸 SHA-256 作为跨存储关联标识。
4. “忘记我”不能只保证 MySQL 删除成功。引入删除任务 / tombstone：删除期间禁止旧向量事实再次注入；Milvus 删除失败时可重试、可观测，完成后才标记全部删除完成。
5. 用户本人删除与运维管理员删除分开定义接口和授权主体；暂不要求在本批重构整套 OIDC，但接口语义先明确。

### 2.4 修改文件

- `memory/store.py`：修正 `_merge_messages`；引入 message ID / revision / CAS 语义。
- `runtime/persister.py`：为新增消息生成稳定身份和 Turn / Tool Call 关联字段。
- `runtime/engine.py`：从请求开始创建 `run_id / turn_id`，贯穿落库。
- `profile/injector.py`：删除从自由文本寻找身份证号以选定画像的逻辑，改为接收可信 profile key。
- `profile/service.py`：受控任务、删除 tombstone 和删除完成状态。
- `profile/vector.py`：删除可确认 / 可重试 / 可观测；避免失败仅 warning 后就宣称删除完成。
- `application.py`：组合 MemoryService、ProfileService 和后台任务管理器。

### 2.5 验收用例

- 连续两条内容完全相同的用户消息都必须保存。
- 同一请求重放时同一个 `message_id` 不重复写；不同请求即使内容相同也不合并。
- 两个实例同时向同一 session 追加消息：无丢失、无乱序、无工具调用结果错配。
- 在 user message 写入后、assistant message 写入前崩溃：恢复后能够识别未完成 Turn，不产生伪造完整回复。
- Mongo 主存储失败、CAS 冲突、文件备份失败：状态与降级事件符合约定。
- 画像服务无可信 profile key：即使用户在聊天里输入他人身份证号，也不能检索到该人的画像。
- Milvus 删除失败：删除任务保持未完成，旧事实在清理完成前不能再被注入。
- 服务重启后，画像抽取任务不会无限制丢失或重复执行。

**Phase 2 完成标准：** 消息身份与内容解耦；会话顺序跨实例可证明；记忆写入和画像删除都有明确完成态。

---

## Phase 3：Tools 工具契约与执行可靠性（第三优先级）

**目标：工具定义中的安全与运行字段都是真正生效的执行契约，而不是只存在于 DTO 中。**

### 3.1 统一 ToolDefinition 契约

重点文件：`core/tool.py`、`tools/registry.py`、`security/secure_executor.py`。

保留并真正执行以下字段：

| 字段 | 要求 |
|---|---|
| `parameters` / `input_schema` | 支持 JSON Schema 中声明的类型、required、enum、边界 |
| `permissions_required` | 逐项传入权限决策；没有满足所需 scope 不得执行 |
| `timeout_seconds` | 由工具执行协调层强制执行，并区分执行前超时与结果不确定 |
| `effect` / `risk_level` | 决定是否允许自动重试、是否要求审批以及审计级别 |
| `approval_required` | 与全局审批开关、风险策略统一解释，不允许字段声明和实际行为冲突 |
| `idempotency_required` | 写操作必须声明稳定业务幂等键策略 |
| `retry_policy` | 由 ToolCoordinator 统一解释：`never`、`safe_idempotent`、`query_before_retry` |
| `result_schema` | 工具返回后统一进行结构校验，错误结果也有稳定结构 |

注册阶段 `ToolRegistry.validate_contracts()` 必须检查：工具名唯一、风险与副作用一致、写工具审批策略、幂等策略、超时合法性、参数 schema 完整性、required scopes、result schema、retry policy 是否兼容。

### 3.2 执行流水线

建议收敛为固定顺序：

```text
ToolCall
  |
  v
Normalize Arguments
  |
  v
Preflight Policy / Required Scopes
  |
  v
Deterministic Prevalidation
  |
  v
Approval Gate（需要时创建/消费持久化审批）
  |
  v
Schema Validation + Generic Validation
  |
  v
Injection Guard
  |
  v
Persist STARTED（写操作必须成功）
  |
  v
Execute Adapter
  |
  +--> SUCCEEDED
  +--> FAILED（确定未产生副作用）
  +--> UNKNOWN（可能已经产生副作用）
  |
  v
Persist Outcome + Audit + Result Validation
  |
  v
ToolResult back to Loop
```

具体规则：

1. 先做确定性授权预检查，再生成 PENDING 审批，避免无权限请求产生无意义确认窗；消费 token 后再次检查授权和策略。
2. 用户确认的 `canonical_args` 是审批闭环的参数事实来源，不允许模型后续重写改变。
3. `permissions_required` 应逐工具校验。此项是 Tool 执行安全契约的一部分，必须在 Phase 3 落地；Phase 5 再完善真实身份来源、OIDC 与跨系统主体映射。
4. `_validate_generic()` 不应把所有参数强制要求为字符串。它应尊重 JSON Schema 声明的类型，并对字符串字段单独做长度 / 格式检查。
5. `Tool.timed_call()` 只统计耗时，不等于真正 timeout。统一在 ToolCoordinator / Adapter 层落实 timeout。
6. 只读查询按安全的 retry policy 执行；写操作只有在有业务幂等键且远端语义明确时才允许安全重试。
7. `query_before_retry` 必须先查询业务事实；仍然无法确定时保持 UNKNOWN，不能把模型再调用一次视作对账。
8. 写操作执行前的 `STARTED` 生命周期记录必须持久化成功；若关键记录不可写，则拒绝进入外部副作用阶段。
9. 执行后审计失败不能假装副作用回滚；要将任务标记为待核查，并告警 / 对账。

### 3.3 超时和结果状态模型

建议统一业务执行状态：

- `NOT_STARTED`：尚未进入 Adapter。
- `STARTED`：已登记执行，可能正在进行。
- `SUCCEEDED`：业务事实已确认成功。
- `FAILED`：确认没有发生副作用，且失败可安全呈现。
- `UNKNOWN`：超时、连接中断、进程崩溃或回执丢失，不能证明成功或失败。
- `RECONCILING`：正在查询外部业务事实。
- `RECONCILED_SUCCEEDED / RECONCILED_FAILED`：通过对账收敛的结果。

`asyncio` 超时只控制本地等待，不代表网络对端停止处理。预约、取消等写操作必须以 HIS / 业务库的事实查询结果收敛。

### 3.4 修改文件

- `core/tool.py`：完善契约和 typed ToolResult。
- `tools/registry.py`：注册时校验完整契约。
- `security/secure_executor.py`：授权前置、审批顺序、schema 校验、typed generic validator。
- `runtime/engine_phases.py::_execute_tools`：迁移到 ToolCoordinator，统一状态与预算。
- `runtime/approval_execution.py`、`security/approval_service.py`、`repositories/approval_repo.py`：保留 canonical args / 一次性 token / 跨用户和会话隔离。
- `runtime/task_tracker.py`、`repositories/task_repo.py`：写操作前置持久化和 UNKNOWN 状态。
- `service/his_client.py`、`service/his_slot_service.py`、`service/his_reconcile.py`：Adapter timeout、幂等语义和对账接口。

### 3.5 验收用例

- 每个工具缺少任意 required scope 都拒绝执行；审批通过不能绕过授权。
- DENY 策略不创建 PENDING 审批。
- 参数类型、required、enum、最大长度不符合 schema 时拒绝。
- 工具返回与 `result_schema` 不符时作为协议错误处理，不伪装成功。
- 只读工具达到 timeout 后按声明策略重试；写工具 timeout 进入 UNKNOWN。
- 预约请求在外部已成功但响应丢失时，对账能找到原业务单，不产生第二次预约。
- 写操作执行前 TaskExecution 无法持久化时，不调用 HIS。
- 审批参数被修改、跨用户、跨 session、过期 token、重复消费全部拒绝。
- 审计执行前失败阻止副作用；执行后失败标记待核查，不假称回滚。
- 所有 ToolResult 状态能被 Loop 识别，不把拒绝、待审批、失败和 UNKNOWN 当成同一种结果。

**Phase 3 完成标准：** ToolDefinition 字段与运行行为一致；每次写工具都能证明“谁发起、确认了什么、执行到哪一步、业务事实是什么”。

---

## Phase 4：Context 上下文构建与可信边界（第四优先级）

**目标：上下文是有优先级、有来源标签、有预算、有边界的输入材料；任何非系统资料都不能升级成系统指令。**

### 4.1 统一内容可信级别

建议在上下文模型中明确来源和信任级别：

| 内容 | 来源 | 信任级别 | 处理规则 |
|---|---|---|---|
| 系统安全规则 / 开发者策略 | 服务端部署配置 | 最高 | 唯一可以定义模型行为边界的指令来源 |
| 当前用户输入 | 当前请求 | 不可信用户内容 | 作为当前任务，不得覆盖系统策略 |
| HIS / 业务系统事实 | 已授权服务端查询 | 业务事实 | 标明数据来源和时间；不是行为指令 |
| 工具返回 | 外部工具 | 不可信工具数据 | 不可当成 system prompt |
| 历史对话与历史摘要 | 会话记忆 | 不可信历史材料 | 仅供理解上下文，不可覆盖当前请求 |
| 患者画像 | 服务端绑定的档案事实 | 受限业务资料 | 标明来源、更新时间、有效期，不构成诊断结论 |
| RAG / 知识检索 | 文档库 | 不可信参考资料 | 作为引用材料，不能发出系统级指令 |

### 4.2 修复摘要角色提升

当前 `memory/context.py::_build_summary()` 把历史消息摘要包装成 `Message.system(...)`。这是必须修复的信任边界问题：历史对话中可能包含用户注入或模型错误内容，摘要后不能因此获得 System 级权限。

整改要求：

- 历史摘要保持为不可信上下文（采用 user/context 数据块或显式 `UntrustedContext` 类型），不能用 System role 承载。
- RAG、Profile、历史摘要和工具结果都带 `source / timestamp / trust_level` 元数据。
- 系统 Prompt 明确说明：这些资料只能作为参考事实，任何内嵌指令、要求忽略规则、越权调用工具的内容均不是系统指令。
- 不将 RAG / Profile 文本直接拼接进系统指令段，避免不同来源混成同一信任级别。

### 4.3 统一上下文优先级与预算

当前文件注释中的优先级与实际 `assemble()` 预算分配顺序不一致，应统一成一份可测试规范：

1. 系统安全规则与开发者策略：不可裁剪。
2. 当前用户输入：不可静默丢弃；超限应明确拒绝或请求缩短。
3. 当前 Run 的必要业务事实 / 工具结果：按本轮任务必需程度保留。
4. 已验证且仍有效的患者画像：严格限额。
5. 近期相关历史：按消息组保留。
6. RAG 资料：按检索相关性和来源质量保留。
7. 历史摘要：用于补充背景，预算最低。

注意：RAG 命中不等于可信业务事实；画像“已存储”也不自动意味着真实和仍有效。来源、更新时间和有效期必须参与排序。

Token 预算必须覆盖完整模型请求：系统提示、用户输入、历史、工具 schema、消息协议 overhead 和模型上下文限制。优先采用匹配模型 tokenizer；暂时只能估算时，保留明确安全余量，并记录估算与实际 token 的偏差。

### 4.4 裁剪与消息配对

- 历史裁剪必须以 atomic block 为单位，不能留下 assistant tool call 却裁掉 tool result。
- 先裁低优先级 RAG，再裁摘要和非必要历史；不能把当前输入静默截断。
- 摘要应明确标注覆盖的消息序号范围，不能重复合并同一段历史。
- `ContextAssembler` 输出可检查的 `ContextManifest`：每一段内容的来源、保留/裁剪原因、估算 token 数。
- 同一请求在同一快照与配置下，组装顺序应稳定、可回放。

### 4.5 修改文件

- `memory/context.py`：修正文档优先级、摘要 role、来源标签、token 预算与裁剪策略。
- `runtime/engine_phases.py::_prepare_messages`：迁移到 ContextService，统一 RAG / Profile / History 的注入契约。
- `prompts/xiaozhi-prompt.txt`：明确非系统资料不可覆盖系统规则。
- `retrieval/rag.py`：返回来源、文档版本、命中元数据，不把文本直接升级成系统指令。
- `profile/injector.py`：只注入可信主体绑定、有效期内的事实，并保留来源。

### 4.6 验收用例

- 用户在历史消息、摘要、RAG、工具返回中植入“忽略系统规则”等文本，模型不能据此越权。
- 历史摘要不会以 System role 进入模型请求。
- 同样的上下文在同样预算下可重复构建；裁剪原因可从 ContextManifest 解释。
- 工具调用及结果不会因裁剪而断链。
- 超长用户输入不会被静默截断成另一种请求。
- 工具 Schema 计入完整 prompt 预算；上下文长度接近上限时不会因漏算工具定义而请求失败。

**Phase 4 完成标准：** 上下文优先级、角色、来源、预算与裁剪语义统一；所有非系统内容均保持不可信资料身份。

---

## Phase 5：身份与权限完整生产化（后置实施，但仍是上线门禁）

**本阶段不作为当前第一批开发目标。** 等 Phase 1–4 的核心执行链路稳定后，再集中完成认证、主体与患者身份映射、权限治理和审计体系升级。此前保持已有认证配置与生产 fail-closed 门禁，不允许为了本地联调关闭安全校验后带入生产。

整改范围：

1. Bootstrap 共享密钥发 token 不作为生产用户认证方案；生产接入 OIDC 或受信身份网关，服务端决定 subject，不接受客户端任意指定 `subject_id`。
2. `Principal` 是唯一可信身份来源；患者主档关联通过服务端校验的关系表 / HIS 主档完成，绝不从聊天文本中的身份证号推导查询对象。
3. 统一 permission policy：主体、工具、scope、资源、操作、风险、审批记录共同参与决策；每个工具的 `permissions_required` 必须兑现。
4. `profile:forget` 明确区分本人自助删除、客服协助删除和运维管理操作。
5. 身份密钥、HMAC key 和 token signing key 使用生产密钥托管、轮换与审计机制。
6. 做跨租户 / 跨用户 / 跨患者数据隔离的系统级测试。

### 本阶段上线验收

- 客户端无法通过请求体伪造任意 subject / ops 身份。
- 同一账号不能查询未绑定患者的画像或预约。
- 缺少工具 scope 时，无论模型是否调用、用户是否审批，都不能执行。
- 用户只能访问允许范围内的 session / patient / profile。
- 身份服务不可用时，写操作 fail-closed。
- 身份密钥轮换期间，旧 token 与新 token 的兼容策略明确且可审计。

---

## 5. 端到端生产验收矩阵

| 场景 | 必须证明的结果 | 对应阶段 |
|---|---|---|
| 正常问答 | 请求、模型、最终响应与持久化消息关联完整 | 1 / 2 |
| 连续工具调用 | 每轮输入、工具调用、工具结果、最终回复按序完整 | 1 / 2 / 3 |
| 模型空响应 / 异常 | 重试不重复输出已下发内容；最终状态明确 | 1 |
| 总预算耗尽 | 有限时间内结束，不残留后台任务和未关闭状态 | 1 |
| 客户端中断 | 资源和任务状态正确收尾；写操作不误判为取消 | 1 / 3 |
| 两次相同消息 | 两条合法消息都保留；重放只按 message_id 幂等 | 2 |
| 并发同一会话 | 多实例无消息丢失、乱序或 tool call 错配 | 2 |
| 记忆存储故障 | 失败边界、降级事件和恢复行为可解释 | 2 |
| 画像跨用户访问 | 未经服务端绑定的主体无法读取其他患者画像 | 2 / 5 |
| 画像删除 | MySQL 与向量存储删除状态一致，失败可重试 | 2 |
| 工具缺少权限 | 不执行、不产生外部副作用 | 3 / 5 |
| 审批参数被篡改 | 仅按用户确认过的 canonical args 执行 | 3 |
| 写工具超时 | 进入 UNKNOWN，对账前不重试 | 3 |
| HIS 回执丢失 | 对账能识别真实结果，不重复挂号 / 退号 | 3 / 6 |
| Prompt Injection | 历史、摘要、RAG、工具结果不能覆盖系统策略 | 4 |
| Token 接近上限 | 工具 Schema 和协议开销已计量，按优先级确定性裁剪 | 4 |
| 回放 | 可从 request / run / turn / tool / approval 关联到最终业务事实 | 1 / 2 / 3 / 4 |

---

## 6. 实施节奏、批次与停止条件

每个 Phase 拆成独立小批次，不把多类风险混进一个大 PR。推荐交付顺序：

| 批次 | 工作项 | 交付物 | 停止条件 |
|---|---|---|---|
| B0 | 行为基线与测试清单 | 基线记录、流程图、验收矩阵 | 基线可重复 |
| B1 | RunContext、统一 deadline、取消与 finally 收尾 | Loop 生命周期第一批 | 本地回归通过 |
| B2 | 从 mixin 抽取 LoopController / ModelRunner / ToolCoordinator | Loop 职责拆分 | 行为与事件顺序不变 |
| B3 | 阻塞 IO 与任务生命周期状态 | async / bounded IO、写操作 STARTED 保证 | 取消和故障用例通过 |
| B4 | Message ID、Turn ID、revision / CAS | Memory 身份与并发一致性 | 并发和重复消息用例通过 |
| B5 | 画像任务、可信 key、删除 tombstone | 画像可靠性 | 跨用户与删除用例通过 |
| B6 | ToolDefinition 契约与注册期校验 | Tool Contract | 所有工具契约校验通过 |
| B7 | SecureToolExecutor / Timeout / UNKNOWN | 工具执行可靠性 | 写操作故障注入用例通过 |
| B8 | Context trust / summary / token budget | ContextService | 注入与预算用例通过 |
| B9 | OIDC / Principal / Permission 全面整合 | 身份权限生产化 | 安全隔离验收通过 |

**每个批次执行方式：**

1. 先基于远程最新 `main` 检查代码，确认上一个批次已合并。
2. 只修改当前批次明确列出的范围；不顺手进行无关重构。
3. 不由代码修改者替代用户本地回归；交付变更文件、接口变化、迁移脚本和验收清单。
4. 用户本地回归通过并合并到远程 `main` 后，再推进下一批。
5. 若回归发现行为差异，先修复当前批次，不叠加后续阶段。
6. 完成 Phase 1–4 后做一次端到端静态复核与全链路回归；没有实质缺陷时阶段性冻结，不继续为了“更优雅”无限重构。

### 不在本轮优先处理的事项

- 未经基准证明的缓存、并行化和性能优化。
- 微服务拆分、复杂消息总线和无明确需求的分布式锁。
- 大规模更换模型或 Agent Framework。
- 仅为降低文件行数而做的机械拆文件。
- 真实 HIS 未提供合同、沙箱和接口说明之前，不猜测医院端点与业务语义。

---

## 7. 真实上线边界：代码完成不等于生产可用

即使 Phase 1–5 完成，真实患者生产上线仍需单独满足：

1. 真实医院 HIS 沙箱已接通，预约、确认、取消 / 退号、查询和对账语义均已实测。
2. HIS 幂等键、超时、重试、释放预占、患者主档与退号接口得到医院确认。
3. UNKNOWN 对账 Job 已部署到有明确 SLA 的调度环境，具备告警、重入和人工处理流程。
4. TLS、密钥托管、备份恢复、数据库权限、网络隔离、日志脱敏和合规评审已完成。
5. 多实例部署时，session 顺序、限流、审批消费、任务恢复和后台任务执行语义均经验证。
6. 真实流量压测、故障演练和回滚演练通过。

当前本地 HIS / fake HIS 可以证明本系统内部流程和接口契约，不代表真实医院系统已经接通。`IMPLEMENTED_REAL_SLOT_ADAPTERS` 仍为空时，生产门禁不得解除。

---

## 8. 最终阶段性完成定义

本方案不是追求 Agent “拥有更多组件”，而是确保主体 Agent 的关键职责边界和运行事实可靠。

当以下条件全部达成，可以冻结本轮主体 Agent 架构整改：

- `AgentEngine` 只做编排，Loop / Model / Tool / Context / Memory 各自拥有单一职责。
- Run / Turn / Message / ToolExecution / Approval / BusinessOperation 有明确且稳定的身份关联。
- Loop 能处理正常结束、预算耗尽、模型故障、工具故障、客户端取消和进程崩溃。
- Memory 能证明消息不丢、不乱序、不误去重；画像读取与删除有可信身份和完整状态。
- Tool 契约字段全部兑现；审批、幂等、timeout、retry、UNKNOWN 和对账语义一致。
- Context 不提升历史摘要、RAG、Profile 或工具返回的信任级别，Token 预算覆盖真实模型请求。
- 身份与权限体系完成生产接入，跨用户 / 跨患者隔离验收通过。
- 端到端回归与部署环境验收通过，且没有未解释的状态丢失或重复副作用。

**建议本轮从 B0 → B1 开始。先解决 Loop 的统一生命周期和取消 / 超时边界，再逐批拆分 `engine_phases.py`；不要先做大规模类结构重写，也不要把身份体系的完整改造提前挤占当前核心链路整改。**