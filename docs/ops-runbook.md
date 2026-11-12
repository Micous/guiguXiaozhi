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
