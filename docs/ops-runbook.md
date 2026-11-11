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

## 7. 生产启动门禁速查（10 项）

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
10. `rag_ingest_on_startup=false` 且 `rag_require_index=true`。

**当前第 4 项恒不满足**，所以 `production` 必然启动失败 —— 这是刻意的，
真实 HIS 沙箱验收通过前不要放开。
