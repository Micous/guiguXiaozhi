# Python Harness 生产化基线冻结与差异清单

> **冻结基线**：`9822856`（main，2026-11-11，配置加固批次）
> **测试基线**：`./.venv/bin/python -m pytest tests/ -q` → **291 passed / 6 skipped**（离线，1.95s）
> **复核方法**：逐项源码定位 + 进程级启动验证（`APP_ENV=production` 拒绝 / development 启动 health ok）+ 8081 真实对话冒烟
> **依据**：《Python_Harness_生产化深度审计与分阶段整改设计方案.md》V1.0（审计基线 `f7f0ff4`）、《生产就绪评估.md》（基线 `97d8d9e`）、《上线迭代路线图.md》（基线 `2e60c11`）
> **本文定位**：**Phase 0 交付物**。冻结「审计说了什么」与「当前代码实际是什么」的差异，让后续每个 Phase 从可核对的事实出发，不再照着过期文档整改。
> **配套代码**：本批门禁改动见 commit `9822856`。
> **Phase 1 增量复核（`f1f1cbe` / 357 单测）见本文第 7 节**：P0-01 已从 `SOURCE_CONFIRMED` 变为 `PARTIALLY_FIXED` + `RUNTIME_VERIFIED`，审计的 7 条验收用例逐条有代码/真机证据。第 1~6 节仍是 `9822856` 的冻结快照，按历史保留、不改写。

------------------------------------------------------------------------

## 0. 状态标注口径

| 标记 | 含义 |
|---|---|
| `SOURCE_CONFIRMED` | 本轮读源码确认问题仍在，且能定位到具体文件/符号 |
| `PARTIALLY_FIXED` | 已有缓解措施并已由源码确认，但**根因未消除**（必须写清剩余部分） |
| `RUNTIME_VERIFIED` | 有真实运行态证据（进程级启动、HTTP 请求、trace 属性） |
| `EXTERNAL_DEPENDENCY` | 解决该问题的前置条件在仓库外（HIS 接口 / 合规审批 / 部署资源） |
| `NOT_TESTED` | 源码可见，但本轮未做专项验证（渗透、压测、容量、故障注入、合规审计） |

> 路线图 Phase 0 原文只列了 4 种标记。本表补 `PARTIALLY_FIXED`：本批门禁把多项从「未动」变成「已有缓解但根因仍在」，用原 4 种标会丢掉这个关键区别。

**总体结论（复核后不变）**：不满足面向真实患者上线；内网演示仍需完成 Phase 1（身份）+ Phase 2（审批）。本批只加了**门禁**，没有消除任何一条 P0 根因。

------------------------------------------------------------------------

## 1. 已具备、不要重复投入

| 能力 | 当前定位 |
|---|---|
| 输出治理 / 幻觉门控 | `runtime/interceptors.py`（三类拦截器 + CorrectiveRetryScheduler）、`runtime/stream_filter.py`（claim_span 回看窗口与句子裁剪），E1 特征测试 11 项原样通过 |
| engine 分层 | `runtime/engine.py`（run() 6 步纯编排）、`engine_phases.py`、`state.py`、`persister.py` |
| 可靠性 | 空响应重试、模型/工具双级熔断、请求时间预算、优雅停机、有界健康探针 |
| 可观测 | JSON 结构化日志、指标、追踪树、独立审计、响应头 `X-Trace-Id`；日志/trace/审计已统一脱敏（`reliability/redaction.py`） |
| 回归网 | 291 单测（离线）+ 6 集成（`--run-integration` 才跑）+ E2E / A-B 脚本 |
| 患者画像（默认关） | `profile/`（抽取/加密存储/向量/注入/服务），Fernet 加密 id_card、注入脱敏、180 天 TTL、`DELETE /profile/{user_key}` 忘记我 |

------------------------------------------------------------------------

## 2. P0 差异清单（6 项）

| # | 主状态 | 附加 | 当前定位（文件:符号） | 归属 |
|---|---|---|---|---|
| P0-01 身份与会话归属 | `SOURCE_CONFIRMED` | `RUNTIME_VERIFIED` | `server/app.py::create_app`、`server/routes.py` `/xiaozhi/chat`、`/metrics` `/audit` `/trace*` `/tools`、**`server/routes.py::forget_profile`** | Phase 1 |
| P0-02 权限与审批闭环 | `PARTIALLY_FIXED` | `SOURCE_CONFIRMED` | `config.py::Settings.permission_no_ui`、`security/permissions.py::_approvals`、`security/secure_executor.py::no_ui_policy`、`config_guard.py` | Phase 2 |
| P0-03 Mock 号源 | `SOURCE_CONFIRMED` | `EXTERNAL_DEPENDENCY` | `service/slot_service.py::MockSlotService`、`application.py::_build_slot_service`、`config_guard.IMPLEMENTED_REAL_SLOT_ADAPTERS` | Phase 3b |
| P0-04 数据一致性 | `SOURCE_CONFIRMED` | — | `repositories/appointment_repo.py`（异常吞掉 / 无事务 / 无唯一约束） | Phase 3a |
| P0-05 流式输出谎报 | `PARTIALLY_FIXED` | `RUNTIME_VERIFIED` | `runtime/interceptors.py`、`runtime/stream_filter.py`、`tests/unit/test_engine_characterization.py` | Phase 4 |
| P0-06 敏感数据 | `SOURCE_CONFIRMED` | `NOT_TESTED` | `tools/appointment_tools.py`（回显完整身份证号）、`config.py::mysql_password`、`repositories/appointment_repo.py`（明文落库） | Phase 1/3b + 合规 |

### P0-01 身份认证与会话归属缺失

- `server/app.py::create_app`：`FastAPI(...)` 未挂认证中间件，也没有 CORS / 安全响应头配置。
- `server/routes.py` `/xiaozhi/chat`：`memoryId`、`userId` 直接取自请求体，`userId` 缺省 `anonymous`；`memoryId` 即会话主键，未与任何身份绑定。
- 运维端点 `/metrics`、`/audit`、`/traces`、`/tools` 无 scope 保护（2026-11-10 未带凭证实测全部 `HTTP 200`，`/traces` 会列出所有会话的 `input_preview`）。
- **P2 新增面**：`server/routes.py::forget_profile`（`DELETE /profile/{user_key}`，本轮源码确认在 157 行）无鉴权，任何知道 `user_key` 的人可删除他人画像。**Phase 1 收口时必须一并处理，别漏。**
- **剩余**：Phase 1 全部（JWT/OIDC + 服务端签发 session_id + 归属校验 + 运维端点 scope + 前端登录）。

### P0-02 工具权限与用户审批没有形成闭环

- development 下 `permission_no_ui = "allow"` 仍是默认，无交互通道时 ASK 级写操作（预约/取消）直接放行。
- `security/permissions.py::_approvals` 是进程内 `dict`：多副本不共享、重启即失效、无参数哈希与有效期。
- **本批缓解（不是修复）**：`config_guard.validate_production_settings` 在 `APP_ENV=production` 时强制 `permission_no_ui == "deny"`，否则拒绝启动。development 行为完全不变；审批状态机、参数哈希、前端确认框仍缺。
- **剩余**：Phase 2 全套。

### P0-03 预约号源仍为 Mock，缺少真实 HIS 业务事实

- `service/slot_service.py::MockSlotService` 的出诊/剩余号/约满由医生姓名与日期散列决定。
- `application.py::_build_slot_service` 是唯一装配点，当前只有 `mock` 分支；填未实现的名字会明确报错，不会悄悄退回 Mock。
- **本批缓解**：`config_guard.IMPLEMENTED_REAL_SLOT_ADAPTERS` 当前为空 → production 下 Mock 或未实现适配器**恒定拒绝启动**（等于提前完成路线图 Phase 3a 的「装配 Mock 直接启动失败」验收项）。这是刻意的：**没有真实 HIS，就不该存在「已可上线」的生产模式。**
- **剩余**：`EXTERNAL_DEPENDENCY` — 向医院索取 HIS API 文档/认证/幂等语义/沙箱，再实现 `HisSlotService` + `UNKNOWN` 对账 Job（Phase 3b）。

### P0-04 预约仓储的数据一致性与异常处理不达标

- `repositories/appointment_repo.py`：4 处 `except Exception` 静默返回 `None`/`False`，无日志、无错误分类。
- `find_duplicate()` 与 `save()` 分两次连接、无事务；建表语句无唯一索引 → 并发下可插入重复预约。
- 已有缓解（不替代修复）：工具层 `find_duplicate` 兜底、`booking_executed` metadata、claim 截断。
- **剩余**：Phase 3a（事务内查重+写入、幂等键唯一约束、乐观锁、异常分类、迁移机制）。

### P0-05 流式输出可能先于工具结果发出错误成功承诺

- 已完成并经真实联调验证：`runtime/interceptors.py` 三类谎报拦截 + 纠正重试、`runtime/stream_filter.py` claim_span 截断与句子裁剪、三段式落库避免叙述句污染历史。
- 二期 P1 重构后 E1 的 11 个特征测试一个字未改全部通过（行为等价证明）。
- **剩余**：Phase 4 Response Gate —— 写操作期间服务端缓冲模型自由文本，只有业务状态持久化成功才用**结构化结果**生成完成态；UNKNOWN 用固定中性文案。现有门控保留为纵深防御。

### P0-06 敏感数据存储、回显和访问边界不完整

- `tools/appointment_tools.py`：预约详情回显完整身份证号；`repositories/appointment_repo.py`：身份证明文落库；`config.py::mysql_password` 默认值 `123456`。
- 做得好的部分：日志/trace/审计统一脱敏；P2 画像的 `id_card` 用 Fernet 密文、注入段脱敏（默认关）。
- **本批缓解**：production 拒绝弱口令、拒绝「画像开启但缺 Fernet 密钥」的静默降级；**明文回显与明文落库未改**。
- **剩余**：Phase 1/3b（`patient_ref` 替代明文业务键、回显只留尾号）+ PIPL/医疗数据合规评审（`NOT_TESTED`）。

------------------------------------------------------------------------

## 3. P1 差异清单（7 项）

| # | 主状态 | 附加 | 当前定位（文件:符号） | 归属 |
|---|---|---|---|---|
| P1-01 会话存储降级 | `SOURCE_CONFIRMED` | — | `memory/store.py::ResilientConversationStore` / `FileConversationStore`，`application.py` 装配 | Phase 5 |
| P1-02 任务生命周期 | `SOURCE_CONFIRMED` | — | `runtime/state.py::TurnState`（请求级）、`runtime/persister.py`（三段式落库）；无 AgentTask/ToolExecution 持久化 | Phase 5 |
| P1-03 RAG 启动灌库耦合 | `SOURCE_CONFIRMED` | `RUNTIME_VERIFIED` | `application.py::get_application` 内 `await rag.bootstrap()` | Phase 6 |
| P1-04 进程内状态 | `SOURCE_CONFIRMED` | — | `ops/metrics.py`、`ops/lifecycle.py::RateLimiter`、`reliability/resilience.py::CircuitBreaker`、`security/permissions.py::_approvals`、`reliability/trace_store.py`、`ops/audit.py` | Phase 6 |
| P1-05 配置/容器/部署 | `PARTIALLY_FIXED` | `NOT_TESTED` | `config_guard.py`（本批新增）、`config.py`、`Dockerfile`、`.github/workflows`（不存在） | Phase 2/6 |
| P1-06 医疗安全护栏 | `SOURCE_CONFIRMED` | `NOT_TESTED` | 仓库无 `security/medical_safety.py`；无急症分流、无越界拦截、无医疗评测集 | Phase 7 |
| P1-07 测试体系 | `SOURCE_CONFIRMED` | `NOT_TESTED` | `tests/unit/` 291 passed、`tests/integration/` 6 skipped、无 `tests/security/`、无 CI | Phase 6/7 |

**逐项要点**

- **P1-01**：Mongo 故障自动切本地文件，多副本下会话丢失/串号 → 生产应 503 快速失败而非伪装成空历史。
- **P1-02**：`TurnState` 只活在一个请求内；崩溃恢复时无法判断副作用是否已发生。
- **P1-03**：真机日志 `RAG 就绪：33 块 / 4 文件`，灌库发生在 lifespan 内、启动耗时 30–60s，多副本会同时写同一集合 → 拆为离线单写者 Job + 版本化集合 + alias 原子切换。
- **P1-04**：指标/限流/熔断/审批全在内存，trace/audit 写本地 `logs/*.jsonl` → 多实例状态不一致、审计可丢。
- **P1-05**：本批已加生产启动强校验、`app_env`/`slot_service_impl` 字段、号源单一装配点；**仍缺**：`.github/workflows` CI、依赖/镜像扫描、Dockerfile 非 root + HEALTHCHECK、TLS、密钥管理、`.env` 规范。
- **P1-06**：知识库条目无来源/审核人/版本/失效日期；无急症关键词确定性分流；渗透测试、隐私影响评估、容量压测均未做。
- **P1-07**：本批 +32 个测试**全部是配置门禁单测，不等于任何一条 P0 验收通过**。单测数量只是回归网，Phase 验收以审计里的具体用例为准。

------------------------------------------------------------------------

## 4. 旧文档差异（后续整改前必读）

| 文档 | 原基线 | 本轮复核 | 处理方式 |
|---|---|---|---|
| 《生产就绪评估.md》 | `97d8d9e` / 68 单测 | `9822856` / 291 单测 | 头部保留原评估基线并加「复核基线」，正文测试数字更新；结论不变 |
| 《上线迭代路线图.md》 | `2e60c11` / 120 单测 | `9822856` / 291 单测 | 头部、第 1/4/6 节更新；Phase 0 与第一批第 1、2 项打勾 |
| 《Python_Harness_生产化深度审计与分阶段整改设计方案.md》 V1.0 | `f7f0ff4` | `9822856` | **历史方案正文不改**，头部加复核注记指向本文 |
| 《改造进度与后续规划.md》 | 259 单测（二期收尾） | 291 单测 | 追加上线 Phase 0 小节 |

**审计结论里已经变化的部分**（避免照旧文档重复整改）：

1. 输出治理从「120 单测」成长为 291 单测 + 拦截器链插件化（P0-05 `PARTIALLY_FIXED`）。
2. engine 从 918 行单体重构为 212 行纯编排 + phases/state/persister 分层，P1-02 的定位点随之改变。
3. P2 新增患者画像模块：带来新攻击面（`DELETE /profile` 无鉴权），也带来 Fernet 加密与注入脱敏的实践，可在 P0-06 整改时复用。
4. 本批新增 `APP_ENV=production` 门禁：P0-02 / P0-03 / P1-05 从「未动」变为 `PARTIALLY_FIXED`——**是防止误标生产，不是修好了**。

------------------------------------------------------------------------

## 5. 本批已落地（commit `9822856`）

| 改动 | 文件 |
|---|---|
| 生产启动门禁（聚合报全、早于任何外部连接、development 零变更） | `xiaozhi/config_guard.py` |
| `app_env` / `slot_service_impl` 字段、`is_production` | `xiaozhi/config.py` |
| `_build_slot_service` 作为号源唯一装配点 + `validate_production_settings` 接线 | `xiaozhi/application.py` |
| 32 个门禁单测（含 development 零变更、聚合报全、fail-fast、HIS 未落地红线） | `tests/unit/test_production_guard.py` |

**真机验证**：`APP_ENV=production` 进程级拒绝，聚合报出 3 项不合规且未连接 Ollama/Milvus；8082 development 启动 health ok、RAG 就绪 33 块。291 passed / 6 skipped。

------------------------------------------------------------------------

## 6. 停止点与下一批

- **形态 A/B/C 尚未拍板**（决定终点与是否现在就催 HIS）。无论哪种形态，Phase 0 与本批门禁都是共同起点，已完成。
- **建议下一批**：Phase 1 身份认证与会话隔离（3–5 天，无外部依赖）——它是所有后续阶段的地基，且必须把 `DELETE /profile/{user_key}` 一起收口。若目标是 A（内网演示），做完 Phase 1 + Phase 2 即可停。
- **可并行**：Phase 3a（预约状态机/事务/唯一约束）与 Phase 1 代码不冲突（service/repo 层 vs security 层）。
- **同时并行催外部**（决定 C 形态日期，不阻塞上面）：HIS 接口文档与沙箱、PIPL/医疗数据合规咨询、域名/TLS/密钥管理与部署资源。

> 再次强调：本批是**门禁**，不是修复。`permission_no_ui=allow` 在 development 仍是默认值；把 `APP_ENV` 设成 production 只会得到一次清晰的拒绝，不会让系统变安全。

*冻结时间：2026-11-11｜基线提交：`9822856`*


------------------------------------------------------------------------

## 7. Phase 1 增量复核（2026-11-11，基线 `f1f1cbe` / 357 passed / 6 skipped）

> 本节是增量复核，不改写第 1~6 节的 `9822856` 冻结快照。
> 代码批次：`0631f3d`(P1-A) / `c2cd129`(P1-B) / `d216f5f`(P1-C) /
> `5098470`(P1-C2) / `2656078`(P1-D) / `f1f1cbe`(P1-D2)。

### 7.1 P0-01 状态变化

| 项 | 冻结快照（`9822856`） | Phase 1 后（`f1f1cbe`） |
|---|---|---|
| 主状态 | `SOURCE_CONFIRMED` | `PARTIALLY_FIXED` + `RUNTIME_VERIFIED` |
| 身份来源 | 请求体自报 `memoryId`/`userId` | Bearer JWT 的 `sub`；请求体 `userId` 一律忽略 |
| 会话主键 | 客户端自报、可枚举 | 服务端 CSPRNG 签发 `s-` 前缀，MySQL `agent_sessions` 归属表 |
| 跨用户访问 | 无校验 | 路由层归属校验，「不存在 / 越权」统一 403 防枚举 |
| 运维端点 | 全裸奔 | `ops:read`；认证开启时不注册 `/docs` `/redoc` `/openapi.json` |
| 画像删除 | 知道 `user_key` 即可删 | 需 `profile:forget` scope |
| 会话撤销 | 无 | `DELETE /v1/sessions/{id}`（本人可撤、越权 403、撤销后旧 id 403） |
| 生产门禁 | 4 项 | 5 项（新增：必须 `auth_enabled=true` + JWT 强密钥 + bootstrap 凭证） |

### 7.2 审计 P0-01 的 7 条验收用例（逐条证据）

| # | 用例 | 证据 | 结论 |
|---|---|---|---|
| 1 | 未携带凭证访问聊天：401 | 8082 真机 + `test_chat_requires_token` | ✅ |
| 2 | A 用 B 的 `memoryId`：403 且不读历史 | 真机返回「会话不存在或不属于当前用户。」；`test_chat_cross_user_session_forbidden`（校验在 engine 之前） | ✅ |
| 3 | 伪造 `userId=B` 但 JWT `sub=A`：只以 A 为准 | 真机请求体故意传 `userId=hacker` 仍正常；`test_chat_owner_ok_and_identity_from_token` | ✅ |
| 4 | A 查询 B 的 trace/audit：403 | 普通用户访问运维端点统一 403（`test_ops_endpoints_reject_normal_user` 参数化） | ✅（以统一 403 实现） |
| 5 | 普通用户 `/metrics` `/audit` `/tools` 403；运维成功 | 真机 alice 403 / ops-admin 200；BOLA 参数化用例 | ✅ |
| 6 | 过期 / 签名错 / issuer / **audience** 不符：拒绝 | 单测全覆盖；`f1f1cbe` 补 `aud` 校验，真机旧 token（缺 aud）被拒 401 | ✅ |
| 7 | session 撤销后旧 id 不能继续读写 | 真机撤销 200 → chat 403；`test_revoke_session_makes_old_id_unusable` | ✅ |

### 7.3 仍然存在的口径差异与剩余工作（不要误读为「已可上线」）

1. **认证源是过渡态**：`/v1/auth/token` 用 `auth_bootstrap_secret` 换 JWT，属内网演示；正式上线须换 OIDC / API 网关，并删除该端点与前端 `LoginGate.vue`。
2. **认证默认关闭**（`auth_enabled=false`）：8081/5173 现状仍是开放态。`APP_ENV=production` 门禁强制开启认证，但 production 因 Mock 号源恒定不可启动——门禁不是修复。
3. **scope 未细分**：审计建议 `ops:read`/`audit:read`/`trace:read`/`tools:read` 分别授权，当前统一 `ops:read`；运维角色能读全部 trace（按策略授权，非按会话隔离）。
4. **撤销用物理删除**，未实现审计建议的 `status/version/expires_at` 状态机；前端「开启新会话」的 revoke 是 best-effort（失败不阻断）。
5. **未做**：CORS 与安全响应头、token 存 localStorage 的 XSS 风险（正式应 httpOnly cookie）、jti 黑名单 / token 主动失效、会话 TTL 清理、CSRF、渗透测试。归入 Phase 6/7 或下一批。
6. **P0-02 审批闭环、P0-03 真实 HIS、P0-06 明文身份证回显仍未动**——Phase 2 / 3b 是内网演示与真实上线的下一道门。

**结论**：Phase 1 的 7 条验收用例均有代码与真机证据，身份不再由请求体决定；但**不满足面向真实患者上线**，也不等于 M2（写操作安全）已达成。`DELETE /profile/{user_key}` 的无鉴权面已按约定一并收口。
