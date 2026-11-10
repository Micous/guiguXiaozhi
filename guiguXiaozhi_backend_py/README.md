# 硅谷小智 · Python 后端

Spring Boot + LangChain4j 后端的 Python 重写版，**不使用 LangChain / LangGraph 等 Agent 框架**：
智能体循环、工具调用、RAG 检索、对话记忆全部自研，架构与代码风格对齐
`参考项目/lab/mini_harness`。

> 📖 方案与踩坑记录：[PLAN.md](PLAN.md) · [踩坑与优化记录.md](../踩坑与优化记录.md)

## 快速开始

```bash
# 1) 依赖
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2) 配置
cp .env.example .env

# 3) 启动（默认 :8081，与 Java 版 8080 并存）
python run.py
```

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
| 端口 | 8080 | 8081 |

## 目录

```
xiaozhi/
├── core/         消息与工具接口（对应参考项目 core/）
├── models/       Ollama provider、流式解析、质量门控
├── tools/        工具注册、挂号工具、号源服务（唯一 Mock）
├── memory/       对话记忆、上下文组装、记忆整合
├── retrieval/    ★ RAG：解析/分块/索引/BM25/混合检索
├── runtime/      智能体循环与状态
├── security/     权限决策、四层安全流、参数校验
├── reliability/  日志、追踪、重试
├── ops/          指标、审计、生命周期
└── server/       FastAPI 接入层
```

## 评估

```bash
python scripts/eval_rag.py          # 检索召回评估（纯向量 vs 混合检索）
pytest tests/ -v                    # 全量测试
```
