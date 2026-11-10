# 硅谷小智 · Python 后端 ★ 正式后端

Spring Boot + LangChain4j 后端的 Python 重写版，**不使用 LangChain / LangGraph 等 Agent 框架**：
智能体循环、工具调用、RAG 检索、对话记忆全部自研，架构与代码风格对齐
`参考项目/lab/mini_harness`。

> 这是**当前的正式后端**，默认监听 `:8081`，`./start-local.sh` 默认启动它，
> 前端 `/api` 代理默认指向 8081。
> Java 版（`../guiguXiaozhi_backend/`，LangChain4j，:8080）已降级为**遗留实现**，
> 仅用于 A/B 对比与回归参考（`./start-local.sh --java` / `--with-java` / `--compare`）。

> 📖 方案与踩坑记录：[PLAN.md](PLAN.md) · [踩坑与优化记录.md](../踩坑与优化记录.md) ·
> [改造进度与后续规划.md](../改造进度与后续规划.md)

## 快速开始

```bash
# 1) 依赖（Python 3.11+）
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2) 配置（所有项都有默认值，最小化环境可不建 .env）
cp .env.example .env

# 3) 启动（默认 :8081，与 Java 版 8080 并存）
python run.py
```

启动时会连本地 Milvus 做知识库增量灌库（首次约 30s），Milvus 不可用时自动降级、不阻塞启动。

## 与 Java 版的差异

| 维度 | Java 版 | Python 版 |
|---|---|---|
| Agent 框架 | LangChain4j | 无，自研（Harness 分层） |
| HTTP | Spring WebFlux | FastAPI + uvicorn |
| 模型接入 | LangChain4j Ollama 适配器 | httpx 直连 Ollama 原生 `/api/chat` |
| 向量库 | Milvus（LangChain4j 适配器） | Milvus（pymilvus，自研 EmbeddingStore 抽象） |
| 检索 | 纯向量 | **向量 + BM25 混合，RRF 融合** |
| 向量主键 | 随机 UUID（重复灌库会污染） | **内容哈希（幂等）** |
| 跟踪 | 日志 | **Span 追踪树 + `/trace` 可视化页** |
| 可靠性 | 空响应重试 | 空响应重试 + 熔断降级 + 工具级熔断 + 请求时间预算 |
| 可观测 | 日志 | JSON 结构化日志（脱敏）+ 指标 + 独立审计 |
| 端口 | 8080 | 8081 |

## 运维端点

| 路径 | 说明 |
|---|---|
| `POST /xiaozhi/chat` | 对话，`text/stream;charset=utf-8` 裸文本分块（前端零改动）；响应头 `X-Trace-Id` 关联本次追踪 |
| `GET /health` | 存活探针，含优雅停机状态与在途请求数 |
| `GET /ready` | 就绪探针：model / milvus / mysql，**每个探针 2s 超时**，慢依赖不会拖住请求 |
| `GET /metrics` | QPS、P50/P95、错误率、工具成功率、空响应重试率、降级次数、熔断器与限流器状态 |
| `GET /audit` | 最近的权限决策与工具执行审计（已脱敏） |
| `GET /traces?limit=50` | 最近对话的追踪列表 |
| `GET /trace/{trace_id}` | 某次对话的完整 span 树（JSON） |
| `GET /trace` | 追踪可视化页（纯 HTML + 原生 JS） |
| `GET /tools` | 当前注册的工具及其 JSON Schema、风险等级 |
| `GET /docs` | FastAPI 自动生成的 API 文档 |

## 可靠性设计

- **空响应重试**：Ollama 偶发返回 200 但内容为空时自动重试；`OLLAMA_MAX_RETRIES` 可配。
  只在**尚未向用户吐出任何文本**时允许重试，避免前端出现重复文字。
- **模型熔断**：连续失败达到阈值后打开，返回本地规则分导诊建议（只给方向，绝不声称预约成功）。
- **工具级熔断**：某个工具在本轮连续失败 N 次后停用，防止死循环。
- **幻觉防护**：没调工具却声称"已预约 / 已取消成功"时追加提示，并在 trace 与指标留痕；
  覆盖"您的预约已成功取消"这类反序措辞，且**工具确实执行过的总结轮不算幻觉**。
- **历史去完成态**：操作类请求组装上下文时，把历史里"预约成功/已取消"的断言替换为中性短句，
  避免小模型误以为本次操作已办完而跳过工具调用（实测踩坑记录 7.1）。
  只影响发给模型的上下文，持久化记忆与用户可见回答不变。
- **降级**：Milvus 挂 → 跳过 RAG；MongoDB 挂 → 文件记忆；记忆写失败 → 不阻断对话。
- **护栏**：输入超长截断、注入只留痕不误拦，输出敏感信息（身份证/手机号/密钥）统一打码。
- **优雅停机**：SIGTERM 后停止收新请求，等在途请求排空再释放连接。
- **限流**：按 `memoryId + 来源 IP` 滑动窗口限流，`ENABLE_RATE_LIMIT` 可关。

## 评估与对比

```bash
python scripts/eval_rag.py                # 检索召回评估（纯向量 vs 混合检索）
pytest tests/ -q                          # 全量单测（不依赖外部服务，默认跳过集成测试）
python scripts/compare.py --help          # Java vs Python A/B 对比用法
python scripts/e2e_appointment.py --help  # 挂号 → MySQL → 取消 端到端联调
```

**集成测试**打真实 Ollama / Milvus / MySQL / MongoDB，默认全部跳过，需显式开启并指定
一套已启动的**新版**后端（旧进程没有 `/metrics`，夹具会给出明确报错）：

```bash
XIAOZHI_PY_BASE_URL=http://127.0.0.1:8082 \
  pytest tests/integration -q --run-integration
```

**端到端联调**用同一 `memoryId` 连续多轮对话，从追踪树确认工具真的被调用，
并直连 MySQL 核对 `appointment` 表的写入与删除。`--base-url` 可传 Vite 代理地址，
用来验证"前端业务代码零改动"：

```bash
python scripts/e2e_appointment.py --base-url http://127.0.0.1:5174/api \
  --output logs/e2e-report.json
```

**A/B 对比**（两端都要先启动），报告含回答文本、耗时、Python 侧 span 数、检索命中与工具调用：

```bash
python scripts/compare.py \
  --java http://localhost:8080 --python http://localhost:8081 \
  --questions-file tests/eval/ab_questions.txt --timeout 180 \
  --output logs/ab-report.md --json logs/ab-report.json
```

> - `compare.py` 的 `memoryId` 必须是**数字**：Java 版 `ChatForm.memoryId` 是 `Long`，
>   传字符串会直接被 Jackson 拒绝。
> - 若已生成 `logs/e2e-report.json`，A/B 报告会自动附上挂号全链路的落库结论。
> - trace 的 `total_duration_ms` 是**墙钟耗时**（最早 span 开始 → 最晚结束），
>   不是各 span 耗时之和；后者单独记为 `span_duration_sum_ms`。

## 容器化

```bash
docker compose up --build -d
curl http://localhost:8081/health
```

`compose.yaml` 只编排 Python 后端，MySQL / MongoDB / Milvus / Ollama 复用宿主机
`../start-local.sh` 起的容器，通过 `host.docker.internal` 回连。

## 目录

```
xiaozhi/
├── core/         消息与工具接口（对应参考项目 core/）
├── models/       Ollama provider、流式解析、质量门控
├── tools/        工具注册、挂号工具、号源服务（唯一 Mock）
├── memory/       对话记忆、上下文组装
├── retrieval/    ★ RAG：解析/分块/索引/BM25/混合检索
├── runtime/      智能体循环与状态
├── security/     权限决策、四层安全流、参数校验、注入防护、护栏
├── reliability/  结构化日志、脱敏、追踪、重试与熔断
├── ops/          指标、审计、健康检查/限流/优雅停机
└── server/       FastAPI 接入层与追踪可视化页
```

## 配置

全部走环境变量 / `.env`，样例见 `.env.example`。注意
`MILVUS_ALIAS` 默认 `xiaozhi_knowledge_py`，**与 Java 版集合分开**，不要改成 Java 版的
`xiaozhi_knowledge`，否则两套后端会共用同一份向量数据、失去 A/B 对比意义。
