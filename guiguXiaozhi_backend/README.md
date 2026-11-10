# 硅谷小智 · Java 版后端（遗留实现）

> ⚠️ **本目录是遗留实现，不再是正式后端。**
>
> 正式后端是 [`../guiguXiaozhi_backend_py`](../guiguXiaozhi_backend_py)（Python，自研 Harness，
> 不依赖 LangChain / LangGraph），默认监听 **:8081**，由 `./start-local.sh` 默认启动。
>
> 保留本目录的原因：
> - **A/B 对比基线**：`./start-local.sh --compare` 会同时拉起两套后端，产出行为对比报告；
> - **回归参考**：Python 版的工具契约、系统提示词、RAG 阈值都源自这里，行为需要对齐时可比对。
>
> 启动方式（一般用不到）：`./start-local.sh --java`（仅本后端）或 `--with-java`（双跑）。
> 本后端监听 **:8080**。

## 这是什么

Spring Boot 3.2 + LangChain4j 实现的医疗伴诊助手，包含：

- 智能问诊 / AI 分导诊 / 挂号预约与取消（Function Calling）
- RAG 知识库检索（Milvus 向量库）
- 多轮对话记忆（MongoDB）
- 本地大模型接入（Ollama，兼容 OpenAI 协议的服务也可切换）

## 本地化改造说明

原项目依赖阿里云百炼（对话 + 向量）与 Pinecone 云端向量库，已全部改为本地：

| 原依赖 | 现在 |
|---|---|
| 阿里云百炼 `qwen-max` / `qwen-plus` | 本机 Ollama `qwen2.5:7b` |
| 阿里云百炼 `text-embedding-v3` | 本机 Ollama `andersc/qwen3-embedding:0.6b` |
| Pinecone 云端向量库 | 本地 Docker Milvus（standalone） |
| `DASH_SCOPE_API_KEY` / `PINECONE_API_KEY` | 不再需要 |

其中**号源 / 排班查询**是唯一保留的 Mock（外部 HIS 接口本地调不通），经 `SlotService`
接口隔离，接真实系统时替换该 Bean 即可，工具层代码不用动。

踩坑与修复详情见根目录 [`踩坑与优化记录.md`](../踩坑与优化记录.md)。
