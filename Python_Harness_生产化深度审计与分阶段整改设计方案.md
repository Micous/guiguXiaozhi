# guiguXiaozhi Python Harness 生产化深度审计与分阶段整改设计方案

> 版本：V1.0\
> 审计日期：2026-11-10\
> 仓库：`Micous/guiguXiaozhi`\
> 分支：`main`\
> 审计基线：`f7f0ff40f0063313ac3023a0fdf6d8f6fc1fc1ec`\
> 范围：仅 `guiguXiaozhi_backend_py/` Python 后端；Java
> 后端仅作为历史对照，不纳入整改范围。\
> 本轮性质：静态源码审计与设计交付；没有修改仓库代码，没有运行测试，也没有进行渗透、压力或合规认证。

------------------------------------------------------------------------

## 0. 执行摘要

### 0.1 总体结论

Python 版已经具备一个结构清晰的单 Agent Harness 原型：有组合根、Runtime
循环、Tool
Registry、权限执行层、RAG、会话记忆、重试/熔断、Trace、Audit、Metrics、健康探针和测试脚本。近期提交也已修复一批真实联调问题，包括预约成功
metadata、失败工具调用不再误抑制幻觉门控、纠正重试和参数归一化。

但当前状态仍不能定义为真实患者业务的生产系统。核心阻断并非再增加几个
Prompt 或 Agent 框架，而是以下闭环尚未完成：

1.  请求身份可信：`userId`、`memoryId`
    仍由请求体传入，缺少认证主体与会话归属校验。
2.  写操作授权可信：`ASK` 在没有确认 UI 时默认
    `allow`；审批不是绑定具体业务请求的持久化一次性授权。
3.  业务结果可信：号源仍由 `MockSlotService` 提供；没有真实 HIS
    占号、确认、取消和对账。
4.  数据一致性可信：预约查重与写入分离，数据库异常被吞掉；会话存储失败会切换本地文件。
5.  对用户的结果表达可信：Runtime
    流式输出模型文本，纠正重试无法撤回已经发给用户的错误成功承诺。
6.  运维与多实例可信：限流、熔断、审批、指标和部分追踪/审计仍是进程内或本地文件状态。
7.  医疗安全与合规可信：目前有输入/输出护栏和脱敏基础，但还没有患者身份、紧急情况分流、临床审核、留存删除、密钥与数据访问治理闭环。

**建议定位：** 保留现有模块化单体与自研 Harness，不换 Agent
框架、不拆微服务、不急于多
Agent；先完成身份、授权、业务事实和持久化，再做多实例运维、容量与医疗治理。

### 0.2 本次审计的版本边界

-   `main` 当前 HEAD：`f7f0ff40f0063313ac3023a0fdf6d8f6fc1fc1ec`。
-   Python 后端已被仓库近期提交明确升为正式后端；Java
    版降为遗留对照实现。
-   `生产就绪评估.md` 的文档基线仍为 `97d8d9e`；`改造进度与后续规划.md`
    的进度描述也早于当前 HEAD。两份文档可作为历史证据，但本方案以当前
    `main` 源码复核结果为准。
-   当前提交之后的真实运行状态、外部 HIS
    合同、部署拓扑和目标司法辖区合规要求，不能仅凭仓库静态代码推断。

### 0.3 范围外事项

本轮不安排： - Java 后端迁移或双后端继续对齐； - 引入
LangChain/LangGraph 等 Agent 框架； - 多 Agent、MCP
大规模扩展、微服务拆分； - 长期画像/记忆自动整合等非生产阻断能力； -
在没有基准数据前进行模型推理或 RAG 性能优化； -
真实患者上线。该目标必须在 P0/P1 及专项验证全部通过后另行审批。

------------------------------------------------------------------------

## 1. 当前 Python Harness 结构与已具备能力

  --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
  层次              当前主要文件                                                                                  当前职责                                            审计判断
  ----------------- --------------------------------------------------------------------------------------------- --------------------------------------------------- ------------------------------------------------------
  组合根 / 装配     `xiaozhi/application.py`                                                                      构造模型、RAG、工具、存储、权限、审计、健康探针和   方向正确；需要把生产依赖装配从原型默认值中剥离
                                                                                                                  Runtime                                             

  HTTP 接入         `xiaozhi/server/app.py`、`server/routes.py`                                                   FastAPI、聊天流、健康检查、Trace/工具/运维端点      缺认证与端点授权；请求体身份不可信

  Harness Runtime   `xiaozhi/runtime/engine.py`                                                                   LLM 循环、工具调度、重试、熔断、事件、会话保存      主循环可保留；缺持久化任务状态和写操作输出闸门

  工具契约          `xiaozhi/core/tool.py`、`tools/registry.py`                                                   工具定义、参数和注册表                              可作为工具边界；需强化输入/输出结构与副作用声明

  安全执行          `xiaozhi/security/permissions.py`、`secure_executor.py`                                       策略判定、参数校验、工具执行和审计                  有基础；默认无 UI 放行与授权闭环需整改

  业务服务          `tools/appointment_tools.py`、`service/slot_service.py`、`repositories/appointment_repo.py`   预约、取消、号源查询、持久化                        Mock 号源和仓储一致性是业务生产化阻断项

  记忆              `memory/context.py`、`memory/store.py`                                                        上下文组装、Mongo/文件/内存存储                     需以可信用户和会话归属为前提；生产不能静默切本地文件

  RAG               `retrieval/rag.py`、`vector_store.py`、`manifest.py` 等                                       文档解析、分块、增量向量化、混合检索                已有较完整基础，保留；需解决启动职责和多副本更新协调

  可靠性            `reliability/resilience.py`、`logging.py`、`redaction.py`                                     重试、熔断、日志脱敏                                可保留；进程内状态不等于集群级可靠性

  运维观测          `ops/audit.py`、`metrics.py`、`lifecycle.py`、`reliability/tracing.py`                        审计、指标、限流、优雅停机、追踪                    单机原型基础齐备；缺集中化、权限隔离和持久化保证
  --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

------------------------------------------------------------------------

## 2. 问题清单：当前证据、风险、目标文件

### P0-01：身份认证与会话归属缺失

**当前证据** - `xiaozhi/server/app.py` 创建 FastAPI
应用，但当前接入层没有可见的认证中间件或统一身份依赖。 -
`xiaozhi/server/routes.py` 的 `/xiaozhi/chat` 从 JSON 请求体读取
`memoryId` / `memory_id` 和 `userId` / `user_id`；缺省用户为
`anonymous`，随后把这些值传给 Runtime。 -
`/metrics`、`/audit`、`/traces`、`/trace/{trace_id}`、`/trace`、`/tools`
等运维/诊断接口在当前路由中没有统一的角色校验入口。

**风险** - 调用方可以伪造用户身份和会话标识。 - 会话历史、Trace、Audit
或工具信息可能跨用户暴露。 -
取消预约等业务动作无法证明操作者与患者之间的授权关系。

**整改文件** -
修改：`xiaozhi/server/app.py`、`xiaozhi/server/routes.py`、`xiaozhi/config.py`。 -
新增建议：`xiaozhi/security/auth.py`、`xiaozhi/security/principal.py`、`xiaozhi/security/session_access.py`。 -
后续持久化：新增 `xiaozhi/repositories/identity_repo.py`
或复用已有数据库访问层。

**目标接口** - `get_current_principal(request) -> Principal`：从受信任的
OIDC/JWT/API Gateway 身份断言构造主体；禁止从请求体读取 `userId`
作为身份。 -
`authorize_session(principal, session_id, action) -> SessionAccessDecision`：检查会话所有者、租户和操作权限。 -
`POST /api/v1/sessions`：由服务端创建会话并返回不可预测的
`session_id`。 - 兼容现有 `/xiaozhi/chat` 时，`memoryId`
只能作为会话定位符；服务端必须先验证其归属，不得把它视为认证凭证。 -
运维接口按 `ops:read`、`audit:read`、`trace:read`、`tools:read`
等权限分别保护；生产环境默认只允许内网/受信任运维身份访问。

**建议数据结构**

``` text
Principal:
  subject_id: str
  tenant_id: str | None
  roles: set[str]
  scopes: set[str]
  auth_method: str
  authenticated_at: datetime
  token_id: str | None

ConversationSession:
  session_id: str
  owner_subject_id: str
  tenant_id: str | None
  created_at: datetime
  expires_at: datetime | None
  status: ACTIVE | CLOSED | REVOKED
  version: int
```

**验收用例** 1. 未携带凭证访问聊天接口：401。 2. 用户 A 使用用户 B 的
`memoryId`：403，且不读取任何历史消息。 3. 请求体伪造 `userId=B`，但 JWT
subject=A：只以 A 为准。 4. 用户 A 查询用户 B 的 trace/audit：403
或按策略返回不可区分的 404。 5. 普通用户访问
`/metrics`、`/audit`、`/tools`：403；运维角色按授权成功。 6. token
过期、签名错误、issuer/audience 不匹配：拒绝。 7. session 被撤销后，旧
session id 不能继续读写。

**退出条件** - 身份由可信认证源提供； -
所有用户数据访问都经过归属校验； - 所有诊断/运维端点均有独立权限策略； -
不再存在由请求体决定的可信用户身份。

------------------------------------------------------------------------

### P0-02：工具权限与用户审批没有形成安全闭环

**当前证据** - `config.py` 的 `permission_no_ui` 默认值为 `"allow"`。 -
`permissions.py` 中未登记工具默认 `DENY`，这是正确的默认拒绝基础；但
`ASK` 需要用户确认。 - `secure_executor.py`
在无审批回调时，`no_ui_policy == "allow"` 会直接放行。 -
`PermissionDecisionEngine` 的 `_approvals`
是进程内集合；`record_approval()`
记录后，执行路径没有形成持久化、带请求绑定的一次性授权验证闭环。即便当前回调执行后记录了审批，也不能证明审批可以安全跨进程、跨重启恢复。

**风险** - 没有 UI 的生产部署会把需确认写操作放行。 -
审批无法可靠绑定具体参数、业务对象和有效期。 -
多副本/重启导致审批状态不一致。 - Prompt
或模型对话不能替代用户明确授权。

**整改文件** -
修改：`config.py`、`security/permissions.py`、`security/secure_executor.py`、`tools/registry.py`、`application.py`。 -
新增：`security/approval_service.py`、`repositories/approval_repo.py`、必要的审批
API 路由。

**目标接口**

``` text
PermissionPolicy:
  tool_name: str
  level: DENY | ASK | AUTO | OVERRIDE
  required_scopes: set[str]
  risk_level: READ | LOW_WRITE | HIGH_WRITE
  enabled: bool

ApprovalRequest:
  approval_id: str
  subject_id: str
  session_id: str
  task_id: str
  tool_name: str
  canonical_args_hash: str
  resource_scope: str
  status: PENDING | APPROVED | REJECTED | EXPIRED | CONSUMED | REVOKED
  expires_at: datetime
  approved_by: str | None
  approved_at: datetime | None
  consumed_at: datetime | None
  idempotency_key: str

ApprovalDecision:
  decision: ALLOW | DENY | REQUIRE_APPROVAL
  approval_id: str | None
  reason_code: str
```

-   `SecureToolExecutor.execute()`
    不应接收模型自述的"用户已经同意"作为审批。
-   写工具先产生
    `PENDING_APPROVAL`，由前端/可信确认渠道显示工具名、患者/业务对象、关键参数和影响。
-   用户确认后，服务端签发短时、一次性 approval
    token；执行时重新计算规范化参数哈希并比对。
-   任何参数变化、主体变化、任务变化、过期、重放或撤销都拒绝执行。
-   生产配置强制 `permission_no_ui=deny`；无确认通道时写操作失败关闭。

**验收用例** 1. 无 UI 情况下 `ASK` 写工具必须拒绝。 2. 用户确认参数 A
后，模型把日期改为 B：审批失效。 3. 审批 token 被重复使用：第二次拒绝。
4. 审批超时、撤销、跨用户使用：拒绝。 5.
进程重启/切换实例后审批状态一致。 6. 未注册工具、未知风险级别、缺少
required scope：拒绝。 7. `OVERRIDE`
仅允许明确的管理员策略，不得由模型或请求参数触发。

**退出条件** - 生产默认拒绝； -
审批持久化、主体/任务/工具/参数绑定、限时、一次性； -
没有用户明确确认，不执行预约、取消等高风险写操作。

------------------------------------------------------------------------

### P0-03：预约号源仍为 Mock，缺少真实 HIS 业务事实

**当前证据** - `application.py` 装配 `MockSlotService`。 -
`service/slot_service.py` 明确标注为 Mock，按本地规则模拟出诊与余号。 -
`appointment_tools.py`
在工具内部查询号源后，执行查重和保存；当前并不存在真实 HIS
的预占/确认/释放/对账接口。

**风险** - 查询有号不等于实际锁定号源。 -
两个并发请求可能同时看到有号并都写入本地记录。 -
本地预约记录成功不代表医院挂号成功。 - 外部 HIS
成功但本地超时，或本地成功但外部失败时，结果可能不确定。

**整改文件** -
修改：`service/slot_service.py`、`application.py`、`tools/appointment_tools.py`、`repositories/appointment_repo.py`。 -
新增建议：`service/his_client.py`、`service/his_slot_service.py`、`service/appointment_service.py`、`repositories/appointment_command_repo.py`。 -
HIS 适配器不得直接散落在 Tool 中；Tool
只负责参数/调用契约，业务服务负责状态机。

**目标接口**

``` text
SlotService:
  query_slots(criteria) -> SlotQueryResult
  hold_slot(criteria, idempotency_key) -> SlotHoldResult
  confirm_hold(hold_id, patient_ref, idempotency_key) -> BookingResult
  release_hold(hold_id, idempotency_key) -> ReleaseResult
  query_booking(external_booking_id) -> BookingStatus

AppointmentService:
  create_booking(command, principal) -> AppointmentResult
  cancel_booking(command, principal) -> AppointmentResult
  reconcile_pending(limit) -> ReconcileReport
```

**预约状态机**

``` text
REQUESTED
  -> PENDING_APPROVAL
  -> HOLDING
  -> HELD
  -> CONFIRMING
  -> CONFIRMED

失败/不确定分支：
  -> REJECTED
  -> EXPIRED
  -> FAILED
  -> UNKNOWN (外部调用结果不确定，等待对账)

取消：
  CONFIRMED -> CANCEL_PENDING -> CANCELLED
                         \-> UNKNOWN / CANCEL_FAILED
```

`UNKNOWN` 不能当作失败，也不能当作成功；必须调用 HIS
查询接口对账后再推进状态。

**建议数据结构**

``` text
Appointment:
  appointment_id: UUID
  subject_id: str
  patient_ref: str              # 内部患者引用，不存明文身份证作为业务主键
  department_id: str
  doctor_id: str | None
  slot_id: str
  visit_date: date
  time_period: str
  status: AppointmentStatus
  external_booking_id: str | None
  idempotency_key: str
  created_at: datetime
  updated_at: datetime
  version: int

AppointmentOperation:
  operation_id: UUID
  appointment_id: UUID
  operation_type: BOOK | CANCEL | HOLD | RELEASE | RECONCILE
  request_hash: str
  external_request_id: str | None
  status: PENDING | SUCCEEDED | FAILED | UNKNOWN
  retry_count: int
  last_error_code: str | None
```

**数据库要求** - 本地预约操作和状态变化必须在事务中完成。 -
为幂等键增加唯一约束；对同一外部 booking id 增加唯一约束。 -
是否允许同一患者同科室同日期同时间段重复预约，应以医院业务规则为准；不要仅靠应用层
`find_duplicate()`。 -
不建议直接把明文身份证号作为索引字段。应采用患者内部
ID；如需去重，可采用受控密钥的 HMAC 指纹，并将原始敏感字段加密保存。 -
取消改为状态迁移和外部 HIS 取消确认，不直接删除记录。

**验收用例** 1. HIS 返回无号：本地不得生成已确认预约。 2. HIS
预占成功、确认成功：本地状态最终为 `CONFIRMED`，有外部 booking id。 3.
HIS 超时但实际已成功：本地进入 `UNKNOWN`，对账后变为
`CONFIRMED`，不重复挂号。 4. 同一 idempotency key 并发提交 20
次：只产生一笔业务预约。 5.
号源预占后用户取消/超时：释放号源；释放失败进入待补偿状态。 6.
取消接口超时：不得向用户说"已取消"；查询 HIS 后确定结果。 7. HIS
返回业务拒绝：保存可审计失败原因，不得伪造成功状态。 8. Mock 只能在
`development/test` profile 使用；`production` profile 装配 Mock
必须启动失败。

**外部依赖** - 真实 HIS 的 API
文档、认证方式、字段映射、幂等语义、预占有效期、取消规则、限流与 SLA
必须由业务方/医院提供。本方案定义适配边界，不虚构 HIS 协议。

------------------------------------------------------------------------

### P0-04：预约仓储的数据一致性与异常处理不达标

**当前证据** - `appointment_repo.py` 的
`find_duplicate()`、`save()`、`delete()`、`list_all()` 对数据库异常采用
`return None`、`False` 或空列表。 - `appointment_tools.py` 将
`find_duplicate()` 与 `save()` 分两步调用。 -
当前实现没有把"查重+占号/写入"收敛到一个数据库事务或唯一约束中。

**整改文件** -
修改：`repositories/appointment_repo.py`、`tools/appointment_tools.py`。 -
新增：`repositories/transaction.py` 或由现有连接管理层提供
`UnitOfWork`。 -
增加迁移：`migrations/`（若项目尚无迁移目录，建立统一迁移机制，不再依赖启动脚本中的临时建表语句）。

**目标接口**

``` text
AppointmentRepository:
  get_by_id(appointment_id) -> Appointment | None
  create(command) -> Appointment
  transition(appointment_id, expected_version, new_status, patch) -> Appointment
  get_by_idempotency_key(key) -> AppointmentOperation | None

UnitOfWork:
  __enter__()
  appointments
  operations
  commit()
  rollback()
```

-   仓储异常应分类抛出
    `RepositoryUnavailable`、`UniqueConflict`、`ConcurrencyConflict`、`DataIntegrityError`；不能把数据库不可用伪装成"没有重复记录"。
-   工具层只把可预期业务错误翻译成用户可理解的
    `ToolResult.fail`；未知基础设施异常要标记为系统错误并触发告警。
-   数据库写入必须有事务边界和乐观锁/version 检查。

**验收用例** 1.
数据库不可用：工具返回明确的系统暂不可用，不得继续执行写入。 2.
并发重复预约：唯一约束/幂等键保证只成功一次。 3.
事务中途失败：预约与操作记录一起回滚。 4.
版本冲突：旧版本更新失败并要求重新读取。 5. `list_all()`
的数据库错误不能伪装成"当前没有预约"。

------------------------------------------------------------------------

### P0-05：流式输出可能先于工具结果发出错误成功承诺

**当前证据** - `runtime/engine.py` 在 `chat_model.stream_chat()`
返回文本 delta 时，立即 `yield TextResponseEvent`。 - 新增的
`inspect_turn()`、`CORRECTIVE_PROMPT`
和纠正重试可以发现并修正部分未调用工具的成功承诺；但如果承诺文本已经作为
delta 发给前端，后续纠正无法撤回。 - 近期成功 metadata
的修复值得保留：只有真实工具执行成功才设置 `booking_executed` /
`cancel_executed`，不能退回只看裸 `tool_calls` 的判断。

**整改文件** -
修改：`runtime/engine.py`、`models/quality.py`、`server/routes.py`。 -
新增建议：`runtime/response_gate.py`、`core/execution_result.py`。

**目标规则** - 对
`预约`、`取消`、`退号`、涉及患者身份信息的操作请求，进入
`WRITE_OPERATION` 输出模式。 -
该模式下模型生成的自由文本先进入服务端缓冲区，不直接向前端发送最终文本。 -
Runtime 完成：权限审批 → 工具执行 → HIS/数据库事实核验 →
状态持久化后，才允许生成并发送最终确认语句。 -
执行期间可发送固定的中性进度事件，例如"正在核验号源/正在提交请求"，但不得发送"已预约成功/已取消"等完成态表述。 -
如果结果为
`UNKNOWN`，只能表达"正在核实结果，请勿重复提交"，不得宣称成功或失败。 -
读取类请求可继续流式输出，但输出仍需经过脱敏与医疗安全策略。 -
工具结果文本、LLM
总结和最终业务状态必须分离；最终状态以业务服务返回的结构化结果为准。

**建议结果契约**

``` text
ToolExecutionResult:
  execution_id: UUID
  tool_name: str
  status: STARTED | SUCCEEDED | FAILED | UNKNOWN | REQUIRES_APPROVAL
  business_resource_id: str | None
  authoritative_status: str | None
  user_safe_message: str
  error_code: str | None
  side_effect_committed: bool
  verified_at: datetime | None
```

**验收用例** 1. 模型在首个 delta
就输出"已为您预约成功"，但没有工具调用：前端在核验前不得收到这句话。 2.
工具执行失败：最终输出不得出现"预约成功"。 3. 工具调用成功但 HIS
状态未知：输出"结果核实中"，不能显示成功。 4. HIS
确认成功且本地持久化成功：才允许输出成功确认。 5.
纠正重试后仍编造成功：由确定性 ResponseGate 拦截，而不是仅追加免责声明。
6. 取消失败或超时：不得输出"已取消"。

------------------------------------------------------------------------

### P0-06：敏感数据存储、回显和访问边界不完整

**当前证据** - `appointment_tools.py`
的预约描述文本会回显完整身份证号。 - `Appointment`
数据对象包含身份证号字段；日志/Trace
脱敏不能代替数据库字段加密和访问控制。 -
会话存储可能包含用户原始输入、工具参数和模型回复。

**整改文件** -
修改：`tools/appointment_tools.py`、`repositories/appointment_repo.py`、`memory/store.py`、`reliability/redaction.py`、`reliability/tracing.py`。 -
新增：`security/pii.py`、`security/encryption.py`、`repositories/retention_repo.py`。

**目标规则** -
对话输出、工具结果、Trace、Audit、日志均不得输出完整身份证号、手机号、访问令牌或密钥。 -
患者身份字段按业务必要性最小化；优先内部 patient_ref。 -
必须存储的敏感字段使用 KMS/受控密钥加密；密钥不放入仓库或普通 `.env`。 -
设计数据保留期限、删除/匿名化流程、访问审计与数据导出流程。 -
不允许把脱敏日志视为"数据库已经合规"。

**验收用例** 1.
输入完整身份证号后，聊天响应、Trace、Audit、日志均只显示掩码。 2.
数据库落盘敏感字段为密文或受控令牌，不能直接检索明文。 3.
无业务授权的主体不能读取患者敏感字段。 4.
过期数据按策略删除/匿名化，并保留必要的删除审计。 5.
密钥轮换后旧数据仍可按受控流程解密并重新加密。

------------------------------------------------------------------------

## 3. P1：生产运行可靠性与可运维性

### P1-01：会话存储故障不能自动切换本地文件作为生产"高可用"

**当前证据** - `memory/store.py` 的 `ResilientConversationStore`
在主存储失败后切换到 `FileConversationStore`。 - Mongo
的部分读写异常会返回空列表或吞掉异常；这可能把存储故障表现成"新会话/空历史"。 -
本地文件不适合作为多副本共享会话的故障转移目标。

**修改文件** - `memory/store.py`、`application.py`、`config.py`。 - 新增
`memory/store_health.py` 或存储健康策略。

**目标** - 生产 profile 下
Mongo/共享持久化不可用时快速失败并返回可恢复错误；不自动切本地文件。 -
`FileConversationStore` 仅限开发/测试。 - Mongo
读写异常必须有明确错误类型和可观测事件。 - 为会话保存加入版本号/CAS
或原子 append，避免并发覆盖式保存丢消息。 - 会话 key 由服务端
`session_id` 绑定 `subject_id`。

**验收** - Mongo 不可用时生产启动/就绪探针失败或聊天返回明确
503；绝不写本地 fallback。 -
两个并发请求写同一会话不会丢失消息或乱序。 -
存储恢复后不需要人工合并两个不同数据源。

### P1-02：Runtime 任务生命周期主要是请求内局部状态

**当前证据** - `runtime/engine.py`
中轮次、工具次数、失败计数、纠正重试次数等主要是单次运行的局部变量。 -
当前没有完整持久化的 Task/Execution/Checkpoint
生命周期；进程退出后难以恢复一个执行中的长任务。

**修改文件** -
`runtime/engine.py`、`core/event.py`、`application.py`。 - 新增
`runtime/task.py`、`runtime/execution.py`、`repositories/task_repo.py`、`repositories/task_event_repo.py`。

**目标数据结构**

``` text
AgentTask:
  task_id: UUID
  subject_id: str
  session_id: str
  status: QUEUED | RUNNING | WAITING_APPROVAL | WAITING_TOOL |
          SUCCEEDED | FAILED | CANCELLED | UNKNOWN
  created_at: datetime
  updated_at: datetime
  deadline_at: datetime | None
  idempotency_key: str

AgentExecution:
  execution_id: UUID
  task_id: UUID
  attempt: int
  runtime_version: str
  model_id: str
  status: RUNNING | SUCCEEDED | FAILED | INTERRUPTED
  started_at: datetime
  ended_at: datetime | None

ToolExecution:
  execution_id: UUID
  task_id: UUID
  tool_call_id: str
  tool_name: str
  args_hash: str
  status: STARTED | SUCCEEDED | FAILED | UNKNOWN
  side_effect_committed: bool
  result_ref: str | None

TaskEvent:
  event_id: UUID
  task_id: UUID
  sequence: int
  event_type: str
  payload_json: json
  created_at: datetime
```

**原则** -
不先引入分布式工作流引擎；以数据库持久化状态机和事件记录满足当前单 Agent
需求。 - 工具执行前先落 `STARTED`，完成后落结果；重启恢复时，`STARTED`
且有副作用的工具执行必须查询业务系统，不可盲目重试。 - 事件序号按 task
单调递增；状态迁移通过条件更新/乐观锁保证。

**验收** 1. LLM 调用期间进程退出：任务标记为
`INTERRUPTED`，按策略可重试。 2.
工具执行成功但进程在写回结果前退出：恢复后先查询业务事实，不重复提交。
3. 等待审批的任务重启后仍可继续或过期关闭。 4.
取消任务后不得继续执行尚未开始的工具。 5.
同一幂等键重复提交任务只产生一个逻辑任务。

### P1-03：RAG 启动灌库与在线服务生命周期耦合

**当前证据** - `application.py` 装配阶段调用 `rag.bootstrap()`。 -
`rag.py` 的 bootstrap 包含解析、分块、Embedding、Milvus 写入和 manifest
更新；启动时同步执行会延长启动，并且多实例可能同时更新同一集合。 - 当前
RAG 已有内容哈希幂等、manifest 增量更新、版本化集合/别名切换和 BM25 +
向量混合检索等能力，应保留。

**修改文件** -
`application.py`、`retrieval/rag.py`、`retrieval/vector_store.py`。 -
新增 `scripts/ingest_knowledge.py` 或独立一次性 Job；增加 RAG
数据版本元数据。

**目标** - Web 服务启动只检查当前知识库版本可用，不执行全量灌库。 -
灌库通过独立命令/Job 运行，使用分布式锁或单写者约束。 -
先构建新版本集合并验证，再原子切换 alias；失败时保留旧版本。 - 记录
`knowledge_version`、源文件 hash、chunker 版本、embedding
模型版本、发布时间和回滚版本。

**验收** - 两个应用实例同时启动不会同时重建/切换知识库。 -
灌库中途失败，线上仍使用旧 alias。 -
新版本评测不达标时可回滚到上一版本。 - Web 服务启动耗时不再包含完整
embedding/灌库耗时。

### P1-04：进程内 Metrics、限流、熔断、审批与本地 Trace/Audit 不支持多实例

**修改文件** -
`ops/metrics.py`、`ops/lifecycle.py`、`ops/audit.py`、`reliability/trace_store.py`、`reliability/tracing.py`、`application.py`。 -
新增共享状态适配器与 OpenTelemetry exporter。

**目标** - Metrics：Prometheus/OpenTelemetry
指标导出；业务指标不含用户身份、患者标识等高基数字段。 -
RateLimiter：多实例部署改用 Redis 或 API Gateway 共享限流；按
subject、IP、租户和接口分层限流。 -
CircuitBreaker：明确哪些状态是实例级、哪些需要服务级协调；不为"共享"而把所有熔断状态都写数据库。 -
Audit：生产审计写入集中、追加式存储，失败必须触发告警；高风险写操作审计不能静默丢弃。 -
Trace：集中式后端，敏感字段在 SDK/export 前脱敏，限制访问和留存期限。

**验收** - 两个实例对同一用户/来源执行限流时总额度一致。 -
实例重启不造成审计记录丢失。 - 运维端能按 trace_id 找到一次请求的
LLM、工具、审批、业务状态和存储结果。 - Metrics 不产生
user_id/session_id 等高基数 label。 - 审计写失败时高风险操作按
fail-closed 策略处理。

### P1-05：配置、容器与部署安全需要生产 profile

**当前证据** - `config.py` 存在 MySQL 默认密码
`123456`，`permission_no_ui` 默认 `allow`。 - `Dockerfile` 当前以简单
Python 启动命令运行，缺少非 root 用户与 HEALTHCHECK 等生产加固。 -
`compose.yaml` 主要适配本地开发依赖，不等于公网生产拓扑。

**修改文件** -
`config.py`、`.env.example`、`Dockerfile`、`compose.yaml`、`run.py`。 -
新增 `config/production.yaml` 或明确的环境变量校验模块。

**目标** - `APP_ENV=production`
时强制校验必填密钥、数据库连接、TLS/代理可信配置、权限默认值和 HIS
endpoint。 - 禁止弱默认密码；缺失生产密钥时启动失败。 - 容器非
root、只读文件系统（按需开放日志卷）、资源限制、健康探针、依赖版本扫描。 -
明确反向代理信任边界，不能盲信 `X-Forwarded-For`。 - 生产 secrets 使用
Secret Manager/KMS，不进入镜像和仓库。

**验收** - 生产配置缺失或弱密码时拒绝启动。 - 镜像以非 root 运行。 -
容器健康检查可区分 liveness 与 readiness。 -
配置、密钥和日志中无明文敏感值。 - 本地开发 compose
与生产部署配置职责分离。

### P1-06：医疗安全护栏尚未达到患者场景要求

**当前证据** - `security/guardrails.py`
有输入长度、注入检测留痕、输出脱敏与幻觉承诺标记。 - `models/quality.py`
主要解决工具调用和预约成功/取消成功幻觉，不是临床安全系统。 -
当前没有完整的紧急症状分流、临床升级、诊断/处方边界和医学知识审核闭环。

**修改文件** -
`security/guardrails.py`、`security/injection.py`、`models/quality.py`、`prompts/xiaozhi-prompt.txt`、`runtime/engine.py`。 -
新增
`security/medical_safety.py`、`service/escalation_service.py`、医疗内容评测集。

**目标** -
先定义产品边界：健康信息解释、分诊辅助、预约服务、诊断建议分别允许到什么程度。 -
紧急症状触发固定的急救/人工就医指引，不等待普通 RAG 回答。 -
不允许模型把一般信息包装成确诊、处方或剂量指令。 -
高风险内容需引用权威来源、显示不确定性，并提供人工/临床升级路径。 - RAG
知识需来源、审核人、版本、生效日期、失效日期和撤回能力。 -
医疗安全规则不能只依赖 system prompt；需要确定性规则和评测。

**验收** - 预设急症问题集全部命中紧急分流。 -
诊断/处方越界问题被拦截或转人工。 - 过期/未审核知识不进入患者回答。 -
每条医学结论可追溯到知识版本和来源。 -
低置信度时明确表达不确定性，不生成虚假确定结论。

> 合规要求需按实际经营地、数据控制者/处理者角色、医院合作模式和部署区域专项确认。若处理美国受
> HIPAA
> 约束的受保护健康信息，需进行适用性评估；若在中国处理个人信息/医疗健康数据，需评估
> PIPL、数据安全及相关医疗行业要求。代码审计不能替代法律意见或第三方合规认证。

### P1-07：测试体系需要从单测扩展到安全、契约、故障和容量验收

**当前已有** - `tests/unit/` 有
Runtime、质量门控、RAG、可靠性、可观测等测试。 - `tests/integration/`
有真实依赖集成测试，默认隔离。 - `scripts/e2e_appointment.py` 与
`scripts/compare.py` 有端到端和行为对比用途。

**新增测试目录建议**

``` text
tests/
  unit/
    test_auth.py
    test_session_access.py
    test_approval_lifecycle.py
    test_response_gate.py
    test_appointment_state_machine.py
    test_repository_transactions.py
    test_pii_encryption.py
    test_medical_safety.py
  integration/
    test_auth_api.py
    test_approval_api.py
    test_his_contract.py
    test_appointment_idempotency.py
    test_session_store_failure.py
    test_audit_durability.py
  contract/
    test_his_schema_contract.py
    test_tool_schema_contract.py
  security/
    test_bola.py
    test_broken_function_level_auth.py
    test_sensitive_data_exposure.py
    test_replay_approval.py
  chaos/
    test_his_timeout_reconciliation.py
    test_process_crash_after_external_success.py
  load/
    locustfile.py 或等效压测脚本
```

**验收原则** - 单测通过不代表生产就绪。 - 集成测试必须使用可控 HIS
sandbox/模拟服务器，覆盖成功、拒绝、超时、重复请求、部分成功和结果不确定。 -
安全测试必须验证对象级授权（BOLA/IDOR）、功能级授权、审批重放、敏感数据泄露。 -
压测前先给出目标并发、P95/P99、错误率、资源预算和模型吞吐假设；不先编造容量指标。 -
故障注入至少覆盖 Mongo/MySQL/Milvus/Ollama/HIS
中断、网络超时、进程重启和磁盘写失败。

------------------------------------------------------------------------

## 4. 统一契约：让 Harness 的每个边界可验证

### 4.1 请求上下文

所有运行时入口统一传递不可由模型修改的 `ExecutionContext`：

``` text
ExecutionContext:
  request_id: UUID
  trace_id: str
  subject: Principal
  session_id: str
  task_id: UUID
  execution_id: UUID
  tenant_id: str | None
  channel: WEB | ADMIN | API
  deadline_at: datetime
  idempotency_key: str | None
  policy_version: str
```

-   `subject` 由认证层注入。
-   `session_id` 由服务端签发并完成归属校验。
-   `task_id` 和 `execution_id` 区分一次用户任务与一次执行尝试。
-   工具参数不得覆盖 Context 中的 subject、tenant、session 或权限字段。

### 4.2 ToolDefinition 增加副作用与风险元数据

保留现有工具注册体系，在 `ToolDefinition` 增加：

``` text
ToolDefinition:
  name: str
  description: str
  parameters: JSONSchema
  effect: READ_ONLY | WRITE
  risk_level: LOW | MEDIUM | HIGH
  required_scopes: list[str]
  approval_required: bool
  idempotency_required: bool
  result_schema: JSONSchema
  timeout_seconds: int
  retry_policy: NEVER | SAFE_IDEMPOTENT | QUERY_BEFORE_RETRY
```

-   只读工具可按最小权限自动执行。
-   写工具必须明确副作用、审批策略和幂等策略。
-   不允许通过模型生成的参数选择权限等级。
-   未声明 effect/risk 的工具在生产环境默认拒绝。

### 4.3 统一工具结果

``` text
ToolResultEnvelope:
  execution_id: UUID
  tool_name: str
  status: SUCCEEDED | FAILED | REQUIRES_APPROVAL | UNKNOWN
  business_status: str | None
  resource_id: str | None
  side_effect_committed: bool
  retryable: bool
  error_code: str | None
  safe_message: str
  evidence_ref: str | None
  completed_at: datetime | None
```

模型只能基于 `safe_message`
组织自然语言，最终业务状态由业务服务和权威系统确认。

### 4.4 三类记忆边界

按《智能体规则.md》的 Harness/记忆治理思路，明确区分： - Working
Memory：当前请求/执行中的临时状态，随 task 生命周期清理。 - Session
Memory：会话消息与短期上下文，必须绑定 subject/session 权限。 -
Long-term
Memory：长期用户画像/偏好等，必须在身份、同意、来源、可删除和留存策略完成后再启用。

本阶段不建议优先实现自动长期记忆整合。先解决身份可信、会话隔离和数据治理。

------------------------------------------------------------------------

## 5. 分阶段修改顺序与依赖关系

### Phase 0：冻结基线与差异复核

**目的：** 避免根据过期文档重复整改。

**文件** - 核对
`生产就绪评估.md`、`改造进度与后续规划.md`、`guiguXiaozhi_backend_py/PLAN.md`。 -
新增建议：`docs/python-production-baseline.md`。

**工作** 1. 将基线固定为当前 `main` SHA。 2.
对照近期提交重新标注已完成项：预约成功
metadata、失败工具调用识别、纠正重试、参数归一化、Python 正式后端。 3.
把所有结论标注为
`SOURCE_CONFIRMED`、`RUNTIME_VERIFIED`、`EXTERNAL_DEPENDENCY` 或
`NOT_TESTED`。 4. 不在此阶段改业务实现。

**验收** - 每个问题都能定位到当前文件/接口。 -
旧文档与当前代码的差异列表完整。 - 不把历史测试数字写成当前已验证结果。

**退出条件：** 基线和范围签字确认。

### Phase 1：身份认证、会话隔离、运维端点保护（P0）

**修改顺序** 1. 新建 `Principal` 和认证依赖。 2. 接入 API
Gateway/OIDC/JWT；先选定唯一可信身份来源。 3. 改造
`routes.py`，移除请求体身份信任。 4. 新增 Session Repository
和归属校验。 5. 对所有运维端点加角色/Scope。 6. 补 BOLA/IDOR 测试。

**退出条件：** P0-01 全部验收通过。未通过不得接真实患者数据。

### Phase 2：审批状态机与工具执行安全（P0）

**修改顺序** 1. 扩展 `ToolDefinition` 的副作用、风险和审批元数据。 2. 把
`permission_no_ui` 生产默认改为 `deny`，并加生产配置启动校验。 3.
新建持久化 Approval Repository。 4. 实现审批请求、用户确认、一次性 token
消费和撤销。 5. 改造 `SecureToolExecutor`，执行前重新验证
subject/task/tool/args hash。 6. 为高风险写工具接入审计强制写入。 7.
补审批重放、参数篡改、跨用户使用测试。

**退出条件：** 没有可信用户确认，写工具绝不会执行。

### Phase 3：预约业务服务、HIS 适配与数据一致性（P0）

**前置依赖：** Phase 1、Phase 2 完成；医院提供 HIS 接口规范与测试环境。

**修改顺序** 1. 定义 HIS API contract 和 sandbox 测试。 2. 抽出
`AppointmentService`，Tool 不直接编排数据库与 HIS。 3. 定义预约状态机和
`UNKNOWN` 对账路径。 4. 引入幂等键、操作记录、事务、唯一约束和乐观锁。
5. 实现 `HisSlotService`；Mock 仅限开发/测试。 6.
取消逻辑从物理删除改为状态迁移。 7.
加密敏感字段并清理工具输出中的明文身份证。 8.
补并发、超时、重复请求、部分成功和对账测试。

**退出条件：** 真实 HIS sandbox 端到端验证通过；没有 HIS
时本阶段不能标记完成，也不能宣称真实挂号生产可用。

### Phase 4：写操作 Response Gate 与结果真实性（P0）

**修改顺序** 1. 引入 `ToolResultEnvelope`。 2. 依据意图与 ToolDefinition
将请求分类为只读/写操作。 3. 写操作模型文本缓冲，不直接流式输出完成态。
4. 工具执行后读取权威业务状态。 5.
仅在权威状态和本地持久化都成功后发送完成态文案。 6. `UNKNOWN`
状态使用固定中性文案并进入对账队列。 7. 补"首个 token
即虚报成功"等流式回归测试。

**退出条件：**
对所有写操作，模型无法在事实核验前向用户表达成功或取消完成。

### Phase 5：存储失败策略与 Runtime 任务可恢复（P1）

**修改顺序** 1. 生产环境关闭 FileConversationStore 自动降级。 2. 修复
Mongo 读写异常吞噬和会话并发覆盖问题。 3. 引入
AgentTask/AgentExecution/ToolExecution/TaskEvent。 4.
工具副作用前后持久化执行状态。 5. 增加崩溃恢复与对账恢复逻辑。 6.
把长任务恢复与普通请求超时分开治理。

**退出条件：**
进程重启不会导致重复预约、丢失任务状态或把存储故障伪装成空历史。

### Phase 6：RAG 发布流程、集中观测与部署加固（P1）

**修改顺序** 1. RAG 灌库移出 Web lifespan，改成单写者 Job。 2.
知识库版本与回滚元数据落库。 3. Audit/Trace/Metric 接入集中化后端。 4.
RateLimiter 接共享存储或 API Gateway。 5. 生产配置校验、Secret
Manager、非 root 容器、TLS/代理策略。 6. CI 增加
lint/type/security/dependency/SBOM/image scan。 7.
补备份恢复演练与审计留存策略。

**退出条件：**
多实例部署下状态一致、审计可查、知识库可回滚、实例故障可恢复。

### Phase 7：医疗安全、容量评估与上线验收（P1 / Release Gate）

**修改顺序** 1. 确认产品医疗边界与人工升级流程。 2.
建立急症分流和医疗内容评测集。 3. 完成临床知识来源审核、版本与失效管理。
4. 完成安全测试、渗透测试、隐私影响评估和适用法规评审。 5.
按明确目标并发和 SLO 做容量测试、故障注入。 6.
建立灰度、回滚、告警、值班与事故响应流程。 7.
由业务、医院、信息安全、法务/合规共同签署上线门禁。

**退出条件：** 所有 P0/P1 验收通过，外部 HIS
与合规依赖完成，安全/容量/恢复演练有可复核报告。

------------------------------------------------------------------------

## 6. 阶段验收总表

  --------------------------------------------------------------------------------------------------
  阶段              交付重点             关键阻断验收                           未通过时的运行边界
  ----------------- -------------------- -------------------------------------- --------------------
  Phase 0           当前基线与差异清单   SHA、文件、问题、状态可追溯            只允许继续设计

  Phase 1           身份与会话隔离       401/403、跨用户访问全部拒绝            不接真实患者数据

  Phase 2           审批与工具安全       无审批写操作全部拒绝，重放拒绝         写工具关闭

  Phase 3           HIS 与预约状态机     HIS sandbox、幂等、UNKNOWN             只允许 Mock 演示
                                         对账、并发唯一性                       

  Phase 4           Response Gate        事实核验前绝不输出写操作成功态         关闭写操作

  Phase 5           持久化与恢复         崩溃恢复不重复副作用、不丢任务         单实例非关键试用

  Phase 6           观测与部署           多实例状态共享、审计持久、可回滚       不公网多副本上线

  Phase 7           医疗安全与容量       专项评测、渗透、容量、合规、演练签字   不面向真实患者上线
  --------------------------------------------------------------------------------------------------

------------------------------------------------------------------------

## 7. 推荐新增/修改文件清单

### 7.1 优先修改的现有文件

  ---------------------------------------------------------------------------------------------------
  文件                                         主要修改
  -------------------------------------------- ------------------------------------------------------
  `xiaozhi/server/app.py`                      认证中间件、Trusted Proxy、安全响应头、生命周期职责

  `xiaozhi/server/routes.py`                   Principal 注入、Session
                                               归属、运维路由鉴权、写操作流式闸门

  `xiaozhi/config.py`                          production profile、弱默认值清除、启动强校验

  `xiaozhi/application.py`                     装配真实生产实现；Mock/File fallback 仅开发测试

  `xiaozhi/runtime/engine.py`                  ExecutionContext、持久化任务、工具状态、ResponseGate

  `xiaozhi/tools/registry.py` / `core/tool.py` ToolDefinition 副作用、风险、幂等、结果 schema

  `xiaozhi/security/permissions.py`            策略版本、主体 Scope、结构化判定

  `xiaozhi/security/secure_executor.py`        审批 token 校验、幂等执行、失败关闭

  `xiaozhi/tools/appointment_tools.py`         只做参数适配和业务服务调用；不直接决定业务事实

  `xiaozhi/service/slot_service.py`            抽象真实 HIS 能力；Mock 隔离

  `xiaozhi/repositories/appointment_repo.py`   事务、异常分类、唯一约束、状态迁移

  `xiaozhi/memory/store.py`                    生产禁用本地 fallback、并发安全、异常透明

  `xiaozhi/retrieval/rag.py`                   在线启动与离线灌库分离

  `xiaozhi/ops/audit.py` / `metrics.py` /      集中化与多实例状态
  `lifecycle.py`                               

  `xiaozhi/security/guardrails.py` /           医疗安全与输出策略分层
  `models/quality.py`                          
  ---------------------------------------------------------------------------------------------------

### 7.2 建议新增文件

``` text
xiaozhi/security/
  auth.py
  principal.py
  session_access.py
  approval_service.py
  pii.py
  encryption.py
  medical_safety.py

xiaozhi/runtime/
  task.py
  execution.py
  response_gate.py

xiaozhi/service/
  appointment_service.py
  his_client.py
  his_slot_service.py
  escalation_service.py

xiaozhi/repositories/
  identity_repo.py
  session_repo.py
  approval_repo.py
  task_repo.py
  task_event_repo.py
  appointment_operation_repo.py
  retention_repo.py

scripts/
  ingest_knowledge.py

tests/
  unit/
  integration/
  contract/
  security/
  chaos/
  load/
```

------------------------------------------------------------------------

## 8. 关键设计决策

1.  **继续模块化单体。**
    当前项目规模和部署形态不需要先拆微服务；通过模块边界、接口契约和数据库事务先把可靠性做实。
2.  **Harness 是控制平面。** LLM 负责理解和生成候选动作；Harness
    负责身份、权限、审批、工具调度、状态持久化、结果核验和审计。
3.  **模型输出不是业务事实。** 预约/取消最终状态只能来自
    HIS/业务服务的权威结果。
4.  **写操作默认失败关闭。**
    无身份、无审批、无幂等、无结果核验时不执行。
5.  **业务状态优先于对话体验。**
    宁可返回"结果核实中"，也不能用自然语言掩盖 UNKNOWN。
6.  **不把 fallback 等同于高可用。** 本地文件
    fallback、多实例各自内存指标、静默吞异常都会制造假健康。
7.  **RAG 已有投入应保留。**
    重点改启动发布治理、来源追溯和评测，不重写检索算法。
8.  **长期记忆后置。**
    先解决身份、授权、会话隔离和保留删除，再引入长期画像。
9.  **不以测试数量替代生产验收。**
    需要业务契约、安全、故障、容量和运维演练共同形成证据。

------------------------------------------------------------------------

## 9. 本轮结论与下一步建议

### 本轮已完成

-   核对仓库与当前 `main` 基线。
-   聚焦 Python 正式后端。
-   复核身份入口、Runtime
    流式输出、工具权限、预约服务、仓储、会话存储、RAG、观测与部署关键文件。
-   将历史评估与当前代码差异显式纳入。
-   形成逐问题的文件、接口、数据结构、修改顺序和验收用例。

### 本轮未执行

-   未修改任何代码或仓库文档。
-   未运行单测、集成测试、E2E、压测或安全测试。
-   未连接真实 HIS。
-   未进行渗透测试、合规审计或生产部署验证。

### 建议的下一步

先单独执行 **Phase 0 的基线冻结与差异清单**，随后按 Phase 1 → Phase 2 →
Phase 3/4 的顺序推进。不要并行启动 HIS
业务重构与权限重构，否则身份、审批和业务结果的边界容易再次混在 Tool 层。

**明确停止点：** Phase 4
完成后，应该做一次端到端生产安全评审；如果目标只是内网演示，达到 Phase
1/2 的安全边界并明确 Mock
号源即可阶段性冻结；如果目标是真实患者预约，则必须继续完成 Phase
3--7，不能以"本地 E2E 跑通"替代真实 HIS 和合规验收。