# 运维手册：灌库 / 回滚 / 抓指标 / 多实例限流 / 审计

> 对应 `上线迭代路线图.md` Phase 6。所有命令都在 `guiguXiaozhi_backend_py/` 下执行，
> 用项目虚拟环境解释器（`.venv/bin/python`）。

------------------------------------------------------------------------

## 1. 知识库灌库（单写者 Job）

Web 进程**不再负责灌库**（`RAG_INGEST_ON_STARTUP=false`，生产强制）。
灌库是一次显式变更，由 Job 执行；索引版本化，可回滚。

```bash
# 1) 只看会灌什么（不连 Milvus / 不写任何东西）
.venv/bin/python scripts/ingest_knowledge.py --dry-run

# 2) 增量灌库（只重建改动过的文件；日常改一两篇 md 用这个）
.venv/bin/python scripts/ingest_knowledge.py

# 3) 全量重建（换分块策略 / 换嵌入模型 / 不确定增量是否收敛时用）
.venv/bin/python scripts/ingest_knowledge.py --force --keep 2

# 4) 看有哪些版本、别名指向哪一版
.venv/bin/python scripts/ingest_knowledge.py --list
```

**退出码**：`0` 成功；`1` 失败（配置/依赖/灌库异常）；`3` 已有任务在跑
（`logs/.ingest.lock` 被占用）；`4` 校验不通过。

`4` 的含义很重要：**别名没有切换，旧索引仍在服务**。新集合行数少于应有块数
（Milvus 可见性还没追上）时会走这条路，隔几十秒重跑即可。

**为什么必须单写者**：多个进程同时 upsert 同一集合 + 互相覆盖进度清单，
会出现"灌了一半的索引"和"清单说 v3、集合其实是 v2"。锁是 `fcntl.flock`
非阻塞独占，第二个进程直接退出 3。

**`--force` 的执行顺序**（这个顺序是安全性的全部）：

```
建新版本集合 → pin 写入到新集合 → 灌库 → count(settle=True) 校验
→ 通过才切 alias → 按版本号清理旧版本
```

任何一步失败，别名都不动。读者（Web 实例）始终读到一个完整版本。

## 2. 回滚

```bash
# 回到上一个版本
.venv/bin/python scripts/ingest_knowledge.py --rollback

# 回到指定版本
.venv/bin/python scripts/ingest_knowledge.py --list
.venv/bin/python scripts/ingest_knowledge.py --rollback --version 3
```

回滚会把进度清单移成 `rag_manifest_<alias>.json.rollback`（日志里有 `[warn]`）。
**这是有意的**：清单描述的是"最新一次灌库"，回滚后它与集合不一致，
留着会让下一次增量灌库误判"文件没变"从而跳过重建。移走后，下一次增量灌库
会按磁盘内容全量收敛。

回滚后**必须**跑一次 `--list` 确认 `← 当前` 落在目标版本
（历史坑：别名解析走错兜底分支时，界面上显示回滚成功、实际仍读新版本，
见 `踩坑与优化记录.md` 21.1）。

## 3. 指标抓取

```bash
# Prometheus 抓取端点（text/plain; version=0.0.4）
curl -s http://127.0.0.1:8081/metrics/prometheus

# 人看的 JSON 视图（含 circuit_breaker / rate_limiter 快照）
curl -s http://127.0.0.1:8081/metrics
```

启用认证后两个端点都需要 `ops:read`。

Prometheus 侧最小配置：

```yaml
scrape_configs:
  - job_name: xiaozhi
    metrics_path: /metrics/prometheus
    static_configs:
      - targets: ["xiaozhi-1:8081", "xiaozhi-2:8081"]
```

**标签纪律**：只允许 `tool`（工具名闭集）与 `outcome`（success/failed）。
不要把用户 ID / 会话 / trace / 科室名加进 label —— 高基数会让 Prometheus
内存爆掉，这是比"指标不够细"严重得多的故障。

值得告警的几个：

| 指标 | 含义 / 处置 |
|---|---|
| `xiaozhi_audit_write_failed_total` > 0 | 审计落不了盘；fail-closed 下工具会全部被拒 —— 先查磁盘与 Mongo |
| `xiaozhi_rate_limit_backend_errors_total` > 0 | 共享限流后端故障，聊天正在 503（不是用户超限） |
| `xiaozhi_storage_unavailable_total` > 0 | 会话存储（Mongo）不可用，`storage_fail_fast` 下是 503 |
| `xiaozhi_hallucination_suspected_total` 突变 | 模型开始谎报完成态，查最近是否换了模型/提示词 |

## 4. 多实例限流

单实例：`RATE_LIMIT_BACKEND=memory`（默认，行为同一期）。

多实例：`RATE_LIMIT_BACKEND=redis` + `REDIS_URL`。启动会先 `PING`，
连不上**直接拒绝启动**（不允许"配了 Redis 却静默用内存"—— 那会让
"额度一致"这个前提无人察觉地失效）。

- 后端故障时聊天返回 **503**（不是 429）：429 会让人以为"用户太快了"；
- 若要可用性优先，可显式 `fail_open=True`（不建议，等于"打挂 Redis 就无限速"）；
- 固定窗口实现，窗口边界可能给出 2× 额度，详见踩坑 21.4。

## 5. 审计

- 默认：本地 JSONL（`AUDIT_LOG_PATH`），**写失败只告警**；
- 生产：`AUDIT_SINK=file,mongo`（集中存储）+ `AUDIT_FAIL_CLOSED=true`
  （写失败就不执行工具）。Mongo 启动先 ping，连不上拒绝启动。

语义边界（不要误解）：

| 审计点 | 失败后果 |
|---|---|
| `permission_decision` / `approval_required`（执行**前**） | 工具**不执行**，返回 `AuditUnavailable` |
| `tool_execution`（执行**后**） | 副作用已发生，只能告警 + 计数，归入对账 |

所以 fail-closed 保的是"没有无痕的写操作"，不是"写操作失败可回滚"。

## 6. 容器

```bash
cd guiguXiaozhi_backend_py
docker compose up --build -d
curl http://localhost:8081/health    # liveness
curl http://localhost:8081/ready     # readiness（依赖 Ollama/Milvus/MySQL/Mongo）
```

- 容器以 `uid=10001` 运行，根文件系统**只读**；只有 `/app/logs`（卷）与
  `/tmp`（tmpfs）可写；
- `HEALTHCHECK` 只探 `/health`（liveness）。**readiness 不要写成
  docker HEALTHCHECK** —— 依赖抖动会触发无意义的容器重启循环，
  把"依赖暂时不可用"放大成"服务不可用"；
- 基础镜像可用 `docker build --build-arg BASE_IMAGE=<镜像站>/library/python:3.11-slim`
  覆盖（内网/离线环境不必改文件）；
- 密钥（`MYSQL_PASSWORD`、`AUTH_SECRET_KEY`、`HIS_API_TOKEN`…）一律环境注入，
  `.dockerignore` 已排除 `.env`。

## 7. 生产启动门禁速查（12 项）

`APP_ENV=production` 会在连接任何外部服务之前聚合校验并拒绝启动：

1. `permission_no_ui` 必须 `deny`；
2. MySQL 口令不得是默认/弱口令；
3. 画像开启时必须配 `PROFILE_SECRET_KEY`；
4. 号源适配器必须在 `IMPLEMENTED_REAL_SLOT_ADAPTERS` 里（当前**为空**）；
5. 认证必须开，且 `AUTH_SECRET_KEY` ≥32 字符、`AUTH_BOOTSTRAP_SECRET` 非空；
6. 审批必须开（`APPROVAL_ENABLED=true`）；
7. `storage_fail_fast=true`；
8. 禁止 `slot_service_impl=local_his`；
9. `audit_fail_closed=true` 且 `audit_sink` 含 `mongo`；
10. `rag_ingest_on_startup=false` 且 `rag_require_index=true`；
11. `rag_require_doc_meta=true`（知识条目必须有来源/审核人/失效日期）；
12. `pii_protection_enabled=true` 且 `pii_secret_key` 非空（预约身份证号必须密文落库 + 指纹查重）。

**当前第 4 项恒不满足**，所以 `production` 必然启动失败 —— 这是刻意的，
真实 HIS 沙箱验收通过前不要放开。

## 8. 医疗安全门控（Phase 7）

产品边界方案 A：只做「信息解释 + 分诊引导」，不做诊断/处方/剂量。

### 8.1 哪些请求不会进模型

| 类型 | 判定 | 输出 |
|---|---|---|
| 急症 | 强信号（叫不醒/大出血/抽搐/半身不遂/误服…）或弱信号 + 主体/当下性/求助口吻 | 固定急救话术（120/110、不要自行驾车、不要喂水喂药、CPR/AED） |
| 自伤 | 自杀意念或方法表露 | 独立话术（120/110 + 心理援助热线 + 不要独处） |
| 越界 | 诊断结论 / 处方 / 剂量 / 报告结论 / 重症预后 | 固定边界话术 + 可代做的三件事 |

这些请求 **0.0x 秒返回**、`model.calls == 0`；话术会落进历史（下一轮上下文不断）。

### 8.2 指标

```bash
curl -s http://127.0.0.1:8081/metrics/prometheus | grep medical_
```

- `medical_emergency_total` / `medical_self_harm_total` / `medical_out_of_scope_total`：
  分流次数。**急症计数是业务看板第一行**——它同时说明"分流在工作"和"有多少用户是按急症主诉进来的"；
- `medical_boundary_prompt_total`：正常医疗咨询里注入了边界提示的次数。

指标突然为 0 而线上有急诊类会话 → 先查 `medical_safety.py` 是否被改坏
（`tests/eval/medical_safety_eval.json` 49 条是行为契约）。

### 8.3 知识条目元数据（合规）

```bash
# 只校验（不连任何外部服务）
python scripts/ingest_knowledge.py --check-meta

# 灌库时强制校验（不合规 → 退出码 5，旧索引继续服务）
python scripts/ingest_knowledge.py --force --strict-meta
```

每个知识 markdown 头部必须有：

```markdown
---
source: 北京协和医院官网 · 就医指南
reviewer: 医务处 张××
reviewed_at: 2026-11-01
expires_at: 2027-11-01
version: 2026-10
---
```

- **过期**（`expires_at` < 今天）会被判不合规：医院指南到期必须复审，不是"上线后一直用旧版"；
- 开发态只告警（启动日志 `知识库有 N 个文件元数据不合规`），production 门禁第 11 项强制；
- 元数据会随 chunk 一起进向量库 `metadata`，检索命中即可溯源。

### 8.4 改动医疗规则时

1. 改 `xiaozhi/security/medical_safety.py`；
2. 跑 `pytest tests/unit/test_phase7_medical_safety.py -q`（评测集 49 条 + 话术合规 + 短路）；
3. 任何一条 expect 漂移，先看该条 `note` 判断是"规则错了"还是"预期该更新"——
   **不要为了让测试变绿改 expect**（那是把契约改掉）；
4. 改固定话术必须同步改合规断言（药名/剂量/诊断词黑名单）。

## 9. 预约敏感字段保护（Phase 8，灰度默认关）

`PII_PROTECTION_ENABLED=true` + `PII_SECRET_KEY=<Fernet key>` 后，预约表的身份证号：

- **落库**存密文（`enc:v1:` 前缀，Fernet 带随机 IV）+ `id_card_hash`（HMAC-SHA256 指纹，查重用）+ `id_card_masked`（展示用）；
- **回显**只给掩码（`610121********2831`），校验失败文案也不再回显原值；
- **落历史**前对 tool 参数与 tool 结果统一打码（`security/masking.py`）。

> 生成密钥：`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`。
> **密钥丢失 = 存量密文不可解**（读回自动降级成掩码展示，不会 500），必须进密钥管理。

### 9.1 开关打开是一次数据迁移，顺序不能颠倒

```bash
# 0) 先跑迁移 006（加 id_card_hash / id_card_masked 两列 + 索引）
python -m xiaozhi.migrations --apply

# 1) 看现状（不写库）
python scripts/pii_protect_appointments.py --status

# 2) 回填存量明文行（幂等，可中断重跑；建议先 --dry-run 看量）
python scripts/pii_protect_appointments.py --backfill --dry-run
python scripts/pii_protect_appointments.py --backfill

# 3) 唯一约束从明文列切到指纹列（有指纹为空 / 有重复活跃预约时会拒绝）
python scripts/pii_protect_appointments.py --switch-index --dry-run
python scripts/pii_protect_appointments.py --switch-index

# 4) 验收（不通过退出码 4）
python scripts/pii_protect_appointments.py --verify
```

退出码：`0` 成功 / `1` 执行失败 / `2` 配置不符（含"未开启保护却来回填"）/ `4` 验收或前置条件不通过。

**为什么唯一约束必须切到指纹列**：Fernet 密文带随机 IV，同一明文两次加密结果不同，
建在 `id_card` 上的唯一索引**永远不冲突**＝去重静默失效。

**为什么回填期间查重 SQL 带 `OR id_card=%s`**：回填是分批的，中途既有密文行也有明文行。
只按指纹查会漏掉未回填的老患者 → 同一个人被重复挂号。

### 9.2 回滚

`PII_PROTECTION_ENABLED=false` 即可让仓储回到一期口径（明文 SQL），
但**已回填的行仍是密文**（读回会解密，功能正常）。要真正退回明文只能从备份恢复——
所以先在预生产库上跑通再动生产。

## 10. 过期数据清理（Phase 8）

审批记录与任务流水会无限增长，需要定期清理（含用户可识别信息，属数据最小化义务）。

```bash
# 演练（只统计，不删除）
python scripts/purge_expired.py --dry-run

# 实际清理：审批 7 天、任务流水 30 天
python scripts/purge_expired.py

# 自定义留存 / 单批行数
python scripts/purge_expired.py --approval-days 3 --task-days 14 --batch-size 500
```

- **审批按 `expires_at` 判定"已死"**：过了有效期的记录无论 `PENDING` 还是 `APPROVED`
  都换不出可执行 token，所以直接按过期时间删，不看 status（`PENDING` 过期同样不可消费）；
- **任务只删终态**（`SUCCEEDED/FAILED/CANCELLED/UNKNOWN`）且超过留存期的，
  先删子表（`task_events` / `tool_executions` / `agent_executions`）再删父表
  ——顺序错了会撞外键；
- **分批删除**（默认 200 行/批）避免长事务锁表；
- 建议 cron 每天凌晨一次，失败退出码 `1` 交给监控告警；
- 表不存在时跳过（未开审批/任务追踪的部署不需要这张表）。

### 8.5 回复出口扫描（Phase 9-A）

入口（8.1）拦"越界请求不生成"，出口拦"正常请求但模型自己漏出用药/诊断"。
两层都在才算闭环：`感冒了怎么办` 入口必须放行，但 7B 仍会说"可以使用一些非处方药物"。

```bash
curl -s http://127.0.0.1:8081/metrics/prometheus | grep medical_output
```

| 指标 | 含义 | 怎么读 |
|---|---|---|
| `medical_output_hits_total` | 命中并丢弃的句子次数（药理/诊断规则 + 半截转折句） | 持续上涨说明模型在漏，先看是不是换了模型版本或提示词被动过 |
| `medical_output_rewritten_total` | 追加了"我不能给用药建议"说明的回复次数 | 与 hits 大致同量级；突增说明回答普遍在讲用药 |
| `medical_output_blocked_total` | **整段**被替换为固定话术的次数 | 非零说明模型整段都在讲药/诊断，属异常信号，要去查该会话 |

行为边界：

- 命中只**丢句**，不重新生成（宁可少说一句，也不让模型再编一遍）；
- 我们自己的合规话术（"不要自行服用任何药物"）带否定词，一律放行；
- 想复现历史行为做对照：`MEDICAL_OUTPUT_GUARD_ENABLED=false`（不推荐用于生产）。

改药名表/规则时：

1. 改 `xiaozhi/security/output_guard.py` 的 `_DRUG_TERMS` / `_DRUG_CLASSES` / 规则正则；
2. 跑 `pytest tests/unit/test_phase9_output_guard.py -q`（40 条，含误伤反例）；
3. **必须先补误伤反例再改规则**：这套规则的历史事故全是"拦对了越界，顺手把正常话删了"
   （泛动词 `用` 把"可以先用物理降温"判成用药建议）；
4. 真机复验用"不带边界提示词"的直连脚本（模拟提示词失效），否则看不出这层有没有在工作。

------------------------------------------------------------------------

## 11. HIS 边界故障演练与处置（2026-11-12）

### 11.1 分类口径（排障时先对表，别急着改状态）

| 客户端异常 | 含义 | 本地状态 | 处置 |
|---|---|---|---|
| `HisUncertainError` | 请求可能已被 HIS 执行 | `UNKNOWN` | **不要手工改状态**，等对账 Job 查业务事实 |
| `HisUnavailableError` | 请求确定没送达（连接拒绝/DNS/429/熔断） | 明确失败 | 检查网关/网络/限流，恢复后用户可重试 |
| `HisAuthError` | token 过期或权限不足 | 明确失败 | 换 `HIS_API_TOKEN`，不要重试 |
| `HisRejectedError` | HIS 明确业务拒绝 | `FAILED` | 如实告知用户（号满/患者不存在等） |
| `HisCircuitOpenError` | 熔断打开，请求未发出 | 明确失败 | 查 HIS 健康；恢复后半开自动探测 |

**红线**：`UNKNOWN` 只能由对账 Job 收敛。任何"看起来像失败"的手工改状态，
在 HIS 其实已落单的情况下都会导致用户重复挂号。

### 11.2 配置项（`Settings`，组合根是唯一装配点）

| 配置 | 默认 | 说明 |
|---|---|---|
| `HIS_API_TIMEOUT` | 10.0 | 单次 socket 超时；整体预算 = timeout × attempts + 1s |
| `HIS_MAX_ATTEMPTS` | 2 | 总尝试次数（含首次），指数退避 + 抖动 |
| `HIS_RETRY_WRITES` | true | **院方未实现幂等时必须设 false**，否则超时重试会重复扣号源 |
| `HIS_BREAKER_THRESHOLD` | 5 | 连续基础设施失败达阈值熔断；业务拒绝不计数 |
| `HIS_BREAKER_RECOVERY_SECONDS` | 20.0 | OPEN → 半开探测的等待时间 |
| `HIS_MAX_RESPONSE_BYTES` | 1048576 | 响应体上限，防止故障 HIS 吐整页 HTML/无限流 |

熔断状态可由 `HisClient.breaker.snapshot()` 观察（`state` / `consecutive_failures`）。
指标走 `metrics.inc` 动态计数：`his_requests_total`、`his_retries_total`、
`his_uncertain_total`、`his_unavailable_total`、`his_rejected_total`、
`his_auth_failure_total`、`his_circuit_open_total`、
`his_malformed_response_total`、`his_oversize_response_total`。

### 11.3 真机演练（改任何 HIS 代码后都要跑）

```bash
# 1. 起沙箱（Docker MySQL 3307 + HTTP 18081）
docker compose -f his_sandbox/docker-compose.yml up -d
./.venv/bin/python -m his_sandbox.seed --reset
/usr/bin/env -i HOME=$HOME PATH=/usr/local/bin:/usr/bin:/bin \
  NO_PROXY='*' no_proxy='*' ./.venv/bin/python -m his_sandbox.server --no-seed

# 2. 打真实 HTTP 的边界演练（19 条，不用 fake server）
NO_PROXY='*' no_proxy='*' ./.venv/bin/python scripts/his_fault_drill.py

# 3. 契约回归（离线，保护既有语义）
./.venv/bin/python -m pytest tests/ -q
```

`detached` 启动必须剥离沙箱注入（`env -i` + `NO_PROXY='*'`），否则本机代理
会把 18081 请求送出去，表现为莫名 502。演练脚本开头会调 `/_admin/reset-fault`
重置 `flaky` / `timeout_once` 计数，可重复跑。

故障注入方式：请求头 `X-His-Fault: <name>` 或环境变量 `HIS_SANDBOX_FAULT`。
pre-route（业务未执行）：`reject_hold`、`http429`、`auth_fail`、`system_busy`、
`pii_leak`、`malformed_json`、`oversize`、`redirect`、`connection_reset`、
`slow_drip`、`flaky`；post-route（业务已提交）：`timeout`、`timeout_once`、
`http500`、`business_fail`。

### 11.4 对账：无外部单号时怎么办

预占超时会落 `external_hold_id = "idem:<幂等键>"`。`ReconcileJob` 见到
`idem:` 前缀会调 `query_booking_by_idempotency_key()`，走 HIS 的
`GET /currentRegister/byKey/{key}`：

- 查到挂号单 → 收敛 CONFIRMED / CANCELLED，并把外部单号补回本地（否则退不了号）；
- 明确查不到 → 收敛 FAILED（确实没挂上，号源由 hold TTL 回收）；
- 查单本身不确定 → 保持 UNKNOWN + `attempts+1`，达 `max_attempts` 告警人工介入。

院方若没有按幂等键查单的端点，这条 UNKNOWN 会走"跳过 + 告警"，**这是保守
设计不是 bug**：没有业务事实就不能猜。接入真实 HIS 时需要与院方确认该能力。

### 11.5 本地 HIS 替身也要单独演练（8081 当前模式）

**两套实现必须分别打**：`his_sandbox`（18081，MySQL）和
`xiaozhi/service/local_his.py`（本机 8081 内嵌，JSON 文件）共用
`HisClient` / `ReconcileJob`，但服务端状态机是各自实现的。只演练沙箱会
漏掉替身侧的缺口（真机复现过：缺 byKey 端点导致对账把 UNKNOWN 误判
成 FAILED）。

启动两个独立进程（务必 `env -i` + `NO_PROXY='*'`，否则本机代理会把
请求送出去表现为 502）：

```bash
cd guiguXiaozhi_backend_py
LOCAL_HIS_LIMIT=200 ./.venv/bin/python -m xiaozhi.service.local_his 18082 &
LOCAL_HIS_HOLD_TTL=1 LOCAL_HIS_LIMIT=200 \
  ./.venv/bin/python -m xiaozhi.service.local_his 18083 &
./.venv/bin/python scripts/local_his_fault_drill.py
```

`LOCAL_HIS_LIMIT` 必须够大：UNKNOWN 用例会**故意**残留预占（那正是要验的
中间态），默认 5 个号跑到中段就号满，后续用例会被误判成"业务拒绝"。
`LOCAL_HIS_HOLD_TTL=1` 用于验证预占过期回收，生产/演示保持默认 600s。

替身侧故障注入只支持写操作：`X-His-Fault: timeout`（业务已提交但不回
响应）、`http500`（业务已提交但返回 500），用于验证"超时 = UNKNOWN +
同幂等键重放收敛"。

| 症状 | 先查什么 |
|---|---|
| 重放被回"预占不存在/已退号" | 请求是否带 `X-Idempotency-Key`；`HisClient` 写操作会自动派生，裸 HTTP 才走严格语义 |
| UNKNOWN 迟迟不收敛 | `ReconcileJob` 是否走 `idem:` 分支；HIS 是否有 `GET /currentRegister/byKey/{key}` |
| 号源越来越少 | 预占 TTL 是否生效；旧持久化文件（无 `expires_at`）重启后会补 TTL |
| 413 变成"不确定" | 服务端是否在**读掉请求体之前**就返 413（TCP RST 会吞掉响应） |
| 查号源恒为空 | 本地替身按日期展开号表，查询必须带 `clinicDate` |

### 11.6 七类接口真机核验（2026-11-12）

在 18081 沙箱上逐项打过，**7/7 接口 + 全流程走通**（可复跑：
`./.venv/bin/python scripts/his_full_flow_check.py`，10/10 通过）：

| # | 接口 | 结果 |
|---|---|---|
| 1 | `GET /clinicForRegist/list` 号源查询 | OK（120 条号表 / 剩余 600） |
| 2 | `POST /clinicForRegist/hold` 预占 | OK |
| 3 | `POST /currentRegister/add` 落单 | OK |
| 4 | `POST /currentRegister/cancel` 退号 | OK |
| 5 | `GET /currentRegister/{id}` 查单 | OK |
| 6 | `POST /patMasterIndex/add` + `GET /patMasterIndex/list` 患者主档 | OK |
| 7 | `GET /dept/list` 科室字典 | OK |
| 8 | `GET /doctor/list` 医生名录（职称/擅长） | OK（院方数据单一源改造新增） |

附加端点：`GET /currentRegister/byKey/{key}`（按幂等键查单）、
`GET /clinicMaster/list`（挂号单列表）同样 OK。

### 11.6.1 院方数据单一源（2026-11-12 架构收敛）

**硬约束：所有院方数据只从 HIS 来**。改造前有两条平行数据链：
`VectorStoreDoctorService` 从知识库 `doctors.md` 经 Milvus 解析科室/医生，
`MockSlotService` 在内存里另造排班；组合根甚至把知识库名录**反向喂给**
本地 HIS（`seed_from_roster`）—— 方向是反的。

现在：HIS 自持 `sys_dept`（科室字典）、`sys_doctor`（医生名录）、
`clinic_index → clinic_schedule → clinic_for_regist`（排班/号源）、
`clinic_master`（挂号单）、`pat_master_index`（患者主档）。
主档定义收敛在 `xiaozhi/service/his_catalog.py`（HIS 域，应用侧不 import）。

| 工具 / 出口 | 数据源 | 适配层 |
|---|---|---|
| 查询科室、工具描述科室清单、系统提示词第 9 条、姓名错位校验 | HIS `/dept/list`、`/doctor/list` | `HisDoctorService`（实现既有 `DoctorService` 抽象） |
| 查询医生（含职称/擅长） | HIS `/doctor/list` | 同上 |
| 查询号源、预约、取消、查单、建档 | HIS REST | `HisSlotService` + `HisClient` |

`doctors.md` 退化为 **HIS 的院方导入源文件**（启动时一次性灌进主档表），
运行时应用侧不读。**工具层 / 业务层 / 提示词一行未改** —— 这是
「接真 HIS 时改动集中」的落点，唯一改动点清单见
`docs/his-sandbox-onboarding.md` 第 0.5 节。

复验：`./.venv/bin/python scripts/his_single_source_check.py`（自包含起服务、
跑完自动关闭），内嵌替身 + Docker MySQL 沙箱 **26/26 通过**。
启动时空科室字典会 fail-fast（`HisCatalogUnavailable` → 拒绝启动），
绝不静默给一份空名单让模型编造科室名。

**全流程正确姿势**（顺序不可换，现已由 `AppointmentService` 自动执行）：
建档 → **用 HIS 返回的 `patientRef`** → 预占 → 落单 → 查单 → 退号。
把本项目本地派生的 `patient_ref` 直接塞给 `confirm_hold` 会被 HIS 拒绝
「患者主档不存在」—— 这不是接口缺失，是引用体系不一致，见 11.7。

### 11.7 已闭合：患者主档引用统一由 HIS 签发（2026-11-12）

原问题：`AppointmentService` 用本地 `sha256(salt|id_card)[:32]` 派生
`patient_ref` 落单，与院方主档编码体系不同 → 独立沙箱真机复现
「患者主档不存在」→ 落单被拒（本地以为约上了、医院根本没有这个患者）。

**现在的做法**：`HisSlotService.patient_master_enabled=True`（组合根在
`local_his` / `his` 模式打开）时，`_book_with_hold` 在**预占之前**先调
`ensure_patient()` 向 HIS 建档，落单统一用 **HIS 返回的 `patientRef`**。
院方数据因此只有一个源，不存在第二份本地引用。

三个边界按下面的方式收敛：

1. **PII 边界**：建档发生在落库**之前**，此时 `id_card` 仍是入参明文，
   不需要从库里解密外送；库里的 `enc:v1:` 密文只在查重/回显链路出现，
   两条路径不交叉。真接院方时若网关要求先查主档，明文窗口仍只在
   入参内存中，不新增持久化解密动作；
2. **幂等键粒度**：建档有自己的 `patient` 前缀幂等键（由身份证号哈希
   派生，不送明文），且排在预占之前 —— 建档失败不会白占号源；
3. **失败分类**：明确 404（院方无建档端点）→ 明确报错、不静默降级；
   超时/不可用/认证失败 → 明确失败（此时还没占号，重试不会重复挂号）；
   绝不退回本地派生引用。

两套 HIS 的落单校验也已对齐（内嵌替身 `require_patient_master` 在
HIS 自持主档时自动打开）。复验见 `scripts/his_single_source_check.py`
的「患者主档引用 → HIS 建档签发」用例：内嵌 + 沙箱 26/26 通过。

**接真 HIS 时**：院方主档编码不同不影响业务层，只要 HIS 建档接口
返回 `patientRef`（或映射到 `_FIELD_MAP`）。

## 12. 首句慢 / 空白气泡怎么排查（2026-11-12）

1. **先分清前端还是后端**：`ChatWindow.vue` 的 `isTyping` 空 bot 气泡说明
   请求已发出、正在等流式首字，不是页面没渲染。
2. **取 trace（需 ops token）**：
   ```
   POST /v1/auth/token  {"subject_id":"<ops 主体>","secret":"<bootstrap>"}
   GET  /traces?limit=5        → 按 input_preview 找到那一轮
   GET  /trace/{trace_id}      → 看 root_spans 的 duration_ms
   ```
   看 `llm.chat` 占整轮比例。>90% 就是模型推理，不是网络 / RAG / Mongo。
   本机 7B 实测 3395 prompt tokens 就要 69 s。
3. **看指标**：纯问候应是 `greeting_fast_path_total +1` 而
   `llm_calls_total +0`；若 `llm_calls_total` 也涨了，说明快路径没生效——
   检查输入是否全匹配短句（夹带业务/身份证会正常进主链路）、以及是否
   被改回旧代码。`/metrics` 同时能看 `medical_emergency_total` 确认急症分流。
4. **别被心跳值误导**：左下「响应延迟 35ms」是探针/心跳，不代表本轮生成耗时。
5. **落库异常**：`memory.write` 阶段若出现 `会话并发写冲突（重试 3 次耗尽）`
   WARNING，说明这一轮回复**没写进历史**（非 strict 模式只告警不阻断）。
   排查是否有多实例/多请求同时写同一 session；生产用 `storage_fail_fast`
   会直接抛 `RepositoryUnavailable`。
