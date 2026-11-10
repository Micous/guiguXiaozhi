# 硅谷小智 · Python 后端重写方案

> 📖 配套文档：[踩坑与优化记录.md](../踩坑与优化记录.md) —— 记录了两轮改造中定位到的真实问题
> （编码、工具调用稳定性、向量库数据污染、RAG 分块与检索策略实验）与最终方案。

> 目标：用 Python 重写硅谷小智后端，**不使用 LangChain / LangGraph 等 Agent 框架**
> （Agent 循环、工具调用、RAG、对话记忆全部自研），架构与代码风格对齐
> `参考项目/lab/mini_harness`，同时完整保留现有 Java 版已验证的业务能力。
> Java 版代码 **不做任何修改**，两套并存。
>
> **不用 Agent 框架的三个目的**（决定了本方案的形态）：
> 1. 搞明白 Agent 的完整流程 —— 每一步都自己写出来，不藏���框架黑盒里；
> 2. 后续真正用上框架时，能清楚对比"框架替我做了什么"；
> 3. 随时能看到完整跟踪流程 —— Tracing 是本项目的一等功能，不是日志副产品。

---

## 一、约束

| 约束 | 落地方式 |
|---|---|
| **不用 LangChain / LangGraph 等 Agent 框架** | Agent 循环、工具调用、RAG 检索、对话记忆**全部自研**，只依赖通用库（httpx / pydantic / FastAPI / pymilvus / pymongo / mysql-connector）。自研部分对齐 MiniHarness 的抽象设计 |
| 不影响 Java 版 | 新目录 `guiguXiaozhi_backend_py/`，独立端口（默认 8081），独立进程 |
| 参考 MiniHarness | 分层、包结构、`Tool` ABC + dataclass + async generator 事件流、`ToolRegistry`、`Config` 单例、`application.py` 组合根、`pytest` + fake 测试 |
| 前端零改动 | 响应必须是 `text/stream;charset=utf-8` + chunked **裸文本分块**（不能是 SSE `data:` 帧） |

> **重点**：去掉 LangChain 后，Java 版依赖 LangChain4j 提供的那些抽象必须自己写一套最小实现，
> 这正是本次重写的主体工作量（详见"三之二、自研抽象层"）。

## 二、目录结构

```
guiguXiaozhi_backend_py/
├── README.md                 # 启动方式、与 Java 版差异、依赖说明
├── PLAN.md                   # 本方案
├── pyproject.toml            # 包声明 / pytest / mypy / black（对齐参考项目）
├── .env.example              # 环境变量样例
├── run.py                    # 启动入口
├── xiaozhi/
│   ├── __init__.py
│   ├── config.py             # 配置单例（对应 application.properties + 参考项目 utils/config.py）
│   ├── application.py        # 组合根（对应 Spring 容器 + 参考项目 application.py）
│   ├── core/                 # ← 参考项目 core/
│   │   ├── message.py        #   Message / Role / MessageType / ToolCall
│   │   ├── tool.py           #   Tool ABC / ToolResult / ToolDefinition
│   │   └── event.py          #   事件定义（对应 runtime/events.py）
│   ├── models/               # ← 参考项目 models/
│   │   ├── ollama.py         #   OllamaProvider：httpx 调 /api/chat，stream + tools + /api/embed
│   │   ├── parser.py         #   NDJSON 增量解析 → TextChunk / ToolCall
│   │   └── quality.py        #   空响应检测 / 工具调用校验 / 幻觉防护
│   ├── tools/                # ← 参考项目 tools/
│   │   ├── registry.py       #   ToolRegistry + schema 缓存
│   │   ├── appointment.py    #   预约 / 取消 / 查号源 / 查专家
│   │   └── slot_service.py   #   SlotService 接口 + MockSlotService（唯一 Mock）
│   ├── memory/               # ← 参考项目 memory/
│   │   ├── store.py          #   ConversationStore / UserProfileStore + Mongo + 文件双实现
│   │   ├── context.py        #   上下文组装（优先级 + token 预算 + 裁剪）
│   │   └── consolidation.py  #   记忆整合（提炼长期画像）
│   ├── retrieval/            # ★ RAG 生产化（对应 Java 的 EmbeddingStoreConfig + Retriever）
│   │   ├── parser.py         #   Markdown 结构化解析（标题层级 / 表格 / 列表 / 元数据）
│   │   ├── chunker.py        #   层级感知分块 + token 预算 + overlap + 稳定块 ID
│   │   ├── manifest.py       #   知识库清单（内容哈希 → 增量更新）
│   │   ├── vector_store.py   #   向量库抽象 + Milvus 实现（幂等 upsert / 版本化 / flush）
│   │   ├── bm25.py           #   纯 Python BM25 稀疏检索（中文 bigram）
│   │   ├── hybrid.py         #   稠密 + 稀疏 → RRF 融合
│   │   └── retriever.py      #   对外 ContentRetriever（Top-K / 阈值 / 元数据过滤）
│   ├── repositories/
│   │   └── appointment_repo.py  # 预约读写（MySQL / SQLite）
│   ├── runtime/              # ← 参考项目 runtime/
│   │   ├── engine.py         #   智能体循环：生成→工具→回填→再生成
│   │   └── state.py          #   会话状态 / 消息窗口 / token 预算
│   ├── reliability/          # ← 参考项目 reliability/
│   │   ├── logging.py        #   结构化日志 + 脱敏
│   │   ├── tracing.py        # ★ Span / trace_span / TraceCollector（完整跟踪）
│   │   ├── trace_store.py    # ★ 追踪存储（环形缓冲 + JSONL 落盘）+ 追踪树构建
│   │   └── resilience.py     #   RetryConfig（对齐参考项目）
│   ├── security/             # ← 参考项目 security/
│   │   ├── permissions.py    #   权限决策引擎 + 审计日志
│   │   ├── secure_executor.py#   四层安全流（权限→校验→注入防护→执行）
│   │   ├── validators.py     #   业务规则校验（身份证 / 日期 / 时段 / 科室）
│   │   ├── injection.py      #   提示词注入检测
│   │   └── guardrails.py     #   通用护栏（对齐参考项目）
│   ├── ops/                  # ← 参考项目 reliability/monitoring + 上线运维
│   │   ├── metrics.py        #   指标采集（QPS/延迟/工具成功率/降级次数）
│   │   ├── audit.py          #   审计日志
│   │   └── lifecycle.py      #   健康检查 / 优雅停机 / 限流
│   ├── server/               # ← 接入层
│   │   ├── app.py            #   FastAPI 应用 + 路由（裸文本分块流式）
│   │   ├── routes.py         #   POST /xiaozhi/chat · GET /health
│   │   └── trace_view.py     # ★ 追踪可视化页（纯 HTML+JS，无前端框架）
│   └── knowledge/            # 知识库（自 Java 版 resources/knowledge 复制）
│       ├── 医院信息.md  科室信息.md  神经内科.md  doctors.md
├── config/
│   └── permissions.yaml      # ★ 工具权限策略（热加载）
├── scripts/
│   └── compare.py            # ★ Java(8080/LangChain4j) vs Python(8081/自研) 对比
├── Dockerfile
└── compose.yaml
└── tests/
    ├── unit/                 # 不依赖 Ollama，用 fake
    ├── integration/          # 打本地真实服务
    └── eval/
        └── rag_eval.json     # 检索召回率评估集（问题 → 期望命中标题路径）
```

## 三、Java → Python 能力映射

| Java 版 | Python 版 | 说明 |
|---|---|---|
| `XiaozhiController` | `server/routes.py` + `server/http_server.py` | `Flux<String>` → `AsyncIterator[str]` → socket 分块 |
| `@AiService XiaozhiAgent` | `runtime/engine.py` | 智能体循环，事件流式产出 |
| `@Tool AppointmentTools` | `tools/appointment.py` | Tool ABC + JSON Schema |
| `ChatMemoryProvider` + `MongoChatMemoryStore` | `memory/store.py` | 窗口记忆(20) + Mongo / 文件双实现 |
| `EmbeddingStoreContentRetriever` | `retrieval/retriever.py` | Top-2 / minScore 0.79 / 元数据过滤 |
| `MilvusEmbeddingStore` | `retrieval/milvus_client.py` | v2 REST over urllib |
| `OllamaStreamingChatModel` | `models/ollama.py` | urllib + NDJSON 逐行读 |
| `VectorStoreDoctorService` | `retrieval/doctor_service.py` | 向量库检索专家名单 |
| `SlotService` + `MockSlotService` | `tools/slot_service.py` | 保持"唯一 Mock"的定位 |
| `RetryStreamingChatModel` | `models/quality.py` | 空响应重试 |
| `@Configuration` Bean 装配 | `application.py` | 组合根显式装配 |
| `application.properties` | `config.py` | `.env` + 默认值 |
| Knife4j `/doc.html` | `server/routes.py` | 手写静态 API 文档页 |

## 三之二、自研抽象层（替代 LangChain4j 的部分）

LangChain4j 原本提供的一批抽象，Python 侧需要自己写。设计原则照抄 MiniHarness 的 `core/` 写法：**ABC + dataclass + 纯 Python 类型**。

| 自研抽象 | 替代 Java 的 | 职责 | 文件 |
|---|---|---|---|
| `Message` / `Role` / `MessageType` | `ChatMessage` 系列 | 统一消息模型（user/assistant/tool/system） | `core/message.py` |
| `Tool` ABC / `ToolDefinition` / `ToolResult` | `@Tool` 注解 | 工具抽象 + JSON Schema + 执行结果 | `core/tool.py` |
| `ChatMemoryStore` ABC | `ChatMemoryStore` | 会话消息读写（Mongo 实现 + 文件实现） | `memory/store.py` |
| `MessageWindowChatMemory` | `MessageWindowChatMemory` | 滑动窗口（保留最近 20 条） | `memory/store.py` |
| `EmbeddingStore` ABC | `EmbeddingStore` | 向量写入/检索/维度校验/flush | `retrieval/vector_store.py` |
| `ContentRetriever` ABC | `EmbeddingStoreContentRetriever` | Top-K + 阈值 + metadata filter | `retrieval/retriever.py` |
| `EmbeddingModel` 协议 | `EmbeddingModel` | 向量化（Ollama `/api/embed`） | `models/ollama.py` |
| `ChatModel` / `StreamingChatModel` 协议 | `ChatModel` / `StreamingChatLanguageModel` | 推理（含 tools、stream） | `models/ollama.py` |
| `AgentLoop` | `@AiService` + `DefaultAiServices` | 智能体循环 + 工具调用 + 记忆 + RAG | `runtime/engine.py` |
| `RetrievalAugmentor` | `DefaultRetrievalAugmentor` | 检索结果按中文模板注入用户消息 | `memory/context.py` |

**不自己写的**：HTTP 路由、依赖注入、配置加载、数据校验、ORM —— 这些交给 FastAPI / pydantic-settings / mysql-connector。

---

## 三之三、跟踪（Tracing）设计 ★ 一等功能

不写框架最大的收益就是**每一步都是自己写的，因此每一步都能被观测**。
本项目把"看完整跟踪流程"做成产品功能，而不是只能翻日志。

### 3.3.1 追踪骨架（对齐参考项目 `reliability/tracing.py`）

- `contextvars` 传递 `trace_id` 与"当前 span"，异步并发下不串味
- `Span` 采用 OpenTelemetry/W3C 形态：`trace_id` 16 字节 hex、`span_id` 8 字节 hex、`parent_span_id` 构成树
- `trace_span(name)` 上下文管理器：自动计时、异常捕获、脱敏写属性
- `TraceCollector.get_trace_tree(trace_id)` 按父子关系组装成树

### 3.3.2 埋点覆盖：一次对话的完整步骤

| Span 名 | 何时产生 | 记录的属性 |
|---|---|---|
| `http.request` | 收到请求 | memory_id、message 长度、客户端 |
| `memory.load` | 读取会话历史 | 消息条数、窗口上限 |
| `retrieval.search` | 向量检索 | 命中文本、相似度、Top-K、阈值、metadata filter |
| `context.assemble` | 组装最终提示词 | 注入的各段字符数（提示词/知识/历史） |
| `llm.chat` | 每次模型调用（第 N 轮） | 模型名、耗时、token 数、是否触发工具、空响应重试次数 |
| `tool.execute` | 每个工具调用 | 工具名、入参、出参、耗时、成功与否 |
| `llm.chat.tool_result` | 工具结果回填后的续写 | 回填内容长度 |
| `http.response` | 响应结束 | 总耗时、输出总字符数、span 总数 |

### 3.3.3 可查看：三个入口

| 入口 | 说明 |
|---|---|
| `GET /trace/{trace_id}` | 返回该次对话的完整追踪树（JSON） |
| `GET /traces?limit=50` | 最近 N 次对话的追踪列表 |
| `GET /trace` | **可视化页面**：纯 HTML + 原生 JS 渲染追踪树 / 甘特时间线 / 每步的完整输入输出 |

存储：内存环形缓冲（默认 500 条）+ 可选 JSONL 落盘（`logs/traces/`），重启后仍可回看。

### 3.3.4 与框架对比（服务于"用上框架后看区别"）

Java 版（LangChain4j）保留运行在 8080，Python 版在 8081，`scripts/compare.py` 对同一问题双发：

| 对比维度 | 说明 |
|---|---|
| 检索命中 | 两边命中的知识片段是否一致、相似度差多少 |
| 工具调用 | 调了哪个工具、次数、参数是否一致 |
| 最终回答 | 文本差异、字数差异 |
| 过程可见性 | LangChain4j 内部有几步可观测 vs 本实现暴露的 span 数 |
| 耗时 | 端到端 + 分段 |

---

## 三之四、上线就绪设计

目标：不只是"能跑"，而是**后续迭代能直接顶上线**。四个重点方向各自落到可测的模块。

### 3.4.1 记忆管理（完整实现，不止"存消息"）

| 层级 | 内容 | 存储 | 生命周期 |
|---|---|---|---|
| 工作记忆 | 当前轮的 user / assistant / tool 消息 | 内存 | 单轮 |
| 短期记忆 | 会话窗口最近 N 条（默认 20，对齐 Java 版） | MongoDB `chat_messages` | 会话内 |
| 长期记忆 | 用户画像：姓名、年龄、偏好、既往预约 | MongoDB `user_profiles` | 跨会话 |

- `memory/store.py`：`ConversationStore` / `UserProfileStore` 两个 ABC，**MongoDB 实现 + 文件实现**（Mongo 挂了自动降级，会话不中断）
- `memory/context.py`：`ContextAssembler` 按**优先级 + token 预算**拼装上下文
  优先级：`系统提示 > 长期画像 > RAG 知识 > 短期历史 > 当前输入`；超预算时按优先级从低到高裁剪
- `memory/consolidation.py`：`ConsolidationEngine`，从历史中提炼长期画像（时间门 24h / 会话数门 5 次触发），防止长期记忆无限膨胀
- 写入时机：每轮结束**异步**落库，写失败进重试队列，**绝不阻塞主流程**
- 记忆的每一次读写都进 trace（`memory.load` / `memory.write` span）

### 3.4.2 工具调用权限控制（四层安全流，对齐参考项目 `secure_executor`）

| 层 | 做什么 | 参考实现 |
|---|---|---|
| L1 权限决策 | `PermissionLevel(DENY/ASK/AUTO/OVERRIDE)` + `PermissionDecisionEngine`，按用户+工具+参数指纹缓存授权 | `security/permissions.py` |
| L2 参数校验 | 工具声明的 JSON Schema + 业务规则校验 | 新增 `security/validators.py` |
| L3 注入防护 | 提示词注入模式检测（"忽略以上指令"等）、工具结果中的可疑内容标记 | 新增 `security/injection.py` |
| L4 执行+审计 | 执行并写审计日志（含 trace_id、决策、入参摘要） | `security/secure_executor.py` |

- 工具在 `ToolDefinition` 上声明 `permissions_required` 与 `risk_level`
- 策略**外置为配置文件**（`config/permissions.yaml`），支持热加载，改策略不用改代码
- 默认策略：只读查询工具 `AUTO`；写入类（预约/取消）`ASK`（需用户确认一次，之后按参数指纹缓存）；未知工具 `DENY`
- `ASK` 在无用户交互通道时**降级为拒绝并回传模型**，绝不放行

### 3.4.3 参数校验（三道关，失败回传给模型而非中断）

| 关 | 校验内容 |
|---|---|
| Schema | 类型 / 必填 / 枚举 / 长度（pydantic 生成 JSON Schema） |
| 业务规则 | 身份证 18 位且校验位合法；日期 `YYYY-MM-DD` 且不早于今天；时段 ∈ {上午, 下午}；科室在白名单内；医生名在 Milvus 名单中 |
| 语义合理 | 预约日期是否为门诊日（周末不出诊）；同科室同时段是否重复预约 |

关键设计：**校验错误不抛异常中断，而是作为结构化"工具错误结果"回传给模型**，让模型自行纠正后重试 —— 这比框架默认的抛异常更稳，也更容易在 trace 里看到。

### 3.4.4 失败兜底（分层降级，任何单点故障都不让用户看到 500）

| 故障点 | 兜底策略 |
|---|---|
| Ollama 超时 / 5xx | 指数退避重试 → 熔断器 → 降级为本地规则应答（分诊表） → 兜底文案 |
| Ollama 返回空响应 | 自动重试（Java 版实测 8/8 稳定触发） |
| 工具执行异常 | 捕获 → 结构化错误回传模型 → 模型改述 |
| 工具连续失败 | 单请求内熔断该工具，本轮不再调用（防死循环） |
| MySQL 不可用 | 降级到内存仓储并标 `dirty`，后台补写 |
| MongoDB 不可用 | 降级到文件记忆，会话仍可用 |
| Milvus 不可用 | 跳过 RAG，模型裸回答，span 标记 `degraded` |
| 记忆写入失败 | 不阻塞，入队重试 |
| 上下文超预算 | 按优先级裁剪（先丢历史，再丢知识） |
| 请求超时 | 请求级时间预算，超时即返回已生成的部分 + 提示语 |
| 死循环 | 单请求最大工具调用次数 + 最大轮次硬上限 |

### 3.4.5 运维要求

| 项 | 实现 |
|---|---|
| 健康检查 | `GET /health`（存活）/ `GET /ready`（探测 MySQL/Mongo/Milvus/Ollama 是否就绪） |
| 优雅停机 | SIGTERM → 停止收新请求 → 等待在途请求完成 → 落盘 trace 与记忆 |
| 结构化日志 | JSON 行输出；**敏感字段脱敏**（身份证/密钥/token/手机号全部打码，对齐参考项目 `redaction.py`） |
| 指标 | QPS / P50 / P95 / 错误率 / 工具成功率 / 空响应重试率 / 降级次数（对齐参考项目 `monitoring.py` 的 `ToolMetrics`） |
| 限流 | 按 memoryId 与来源 IP 限流，防刷 |
| 审计日志 | 工具权限决策与执行结果单独落文件，不与普通日志混在一起 |
| 配置 | 全部走环境变量 / `.env`，密钥不落代码 |
| 容器化 | 提供 `Dockerfile` + `compose.yaml`（复用现有 mysql / mongo / milvus） |
| 测试 | 单测覆盖权限决策、参数校验、降级分支、记忆裁剪；集成测试打真实依赖 |

---

## 三之五、RAG 生产化设计 ★

### 3.5.0 Java 版实测到的真实缺陷（本次必须根治）

对 Java 版的 Milvus 集合做实测发现：

```
集合 xiaozhi_knowledge：79 行
去重后唯一块：25 块      → 27 块重复，每块 ×3
```

排查结论：
1. LangChain4j 的 Milvus 映射用**随机 UUID 作为主键** —— 重复灌同一份内容会**新增行**而非覆盖，天然不幂等；
2. "集合是否为空"用**语义探针**判断（拿一句无关文本去检索，命中与否当作空/非空），不可靠；
3. 结果：每次重启都可能重复灌库，知识库里堆着多份同样的向量，**检索时互相稀释相似度**。

### 3.5.1 文档解析（Markdown 结构化）

| 能力 | 说明 |
|---|---|
| 标题层级 | 解析 `#`~`######`，维护**标题路径**（如 `神经内科 / 专家门诊医生`） |
| 段落边界 | 按空行切段，**表格与代码块视为不可分割单元** |
| 列表 | 保留列表项原文，不拆散 |
| 元数据 | 每块带 `file_name` / `heading_path` / `section` / `start_line` / `content_hash` |
| 编码 | UTF-8（含 BOM 剥离）；非法字节替换而非抛错，单文件失败不影响整体 |
| 其他格式 | `.txt` 按段解析；`.pdf` 走可选 `pypdf`（未安装则跳过并告警） |

### 3.5.2 分块（层级感知 + token 预算）

- **按标题层级切**：一个标题下的内容成块，块的正文**带上标题路径前缀**，检索时模型能看到"这是哪一节"
- **超长块再按句子切**：中文标点感知（`。！？；
`），带 **overlap**（默认 80 字）避免语义在边界断裂
- **token 预算**：按中文 ≈ 1 字 ≈ 0.6 token 估算，默认上限 500 token/块
- **表格不拆**：Markdown 表格整块保留
- **块 ID 稳定**：`sha256(file_name + heading_path + text)` —— 同一内容永远同一个 ID，**重复灌库即幂等**

### 3.5.3 索引管理（幂等 + 增量 + 可回滚）

| 能力 | 实现 |
|---|---|
| 幂等灌库 | 块 ID = 内容哈希，写入用 **upsert** 语义；同一份知识库灌 N 次结果一致 |
| 增量更新 | 维护 `manifest.json`（每个源文件的内容哈希 + 块数）；**只有哈希变化**才重建该文件的块，其余不动 |
| 精确删除 | 按 `metadata.file_name` 过滤删除该文件的旧块，再写新块 |
| 真实判空 | 用 `get_collection_stats().row_count`（flush 后）判断，不再用语义探针 |
| 版本化 | 集合名带知识库版本号 `xiaozhi_knowledge_v{N}`；重建 = 建新集合 → 灌数据 → 校验 → **原子切换别名** → 延迟删旧集合；任一步失败则回滚，不留半成品 |
| 失败降级 | Milvus 不可用 → 跳过 RAG 继续服务，span 标 `degraded`；不阻塞启动 |

### 3.5.4 检索（混合检索 + RRF 融合）

纯向量检索对"抽血需要空腹吗"这类**关键词型问题**召回不稳（实测最高分 0.80，但排第一的却是无关的"实名制就诊"）。因此改为**混合检索**：

```
        ┌─ 稠密检索（Ollama 向量 + Milvus COSINE）─┐
query ──┤                                        ├─→ RRF 融合 → 动态 Top-K → 注入提示词
        └─ 稀疏检索（BM25，中文按字/二元切分）───┘
```

- **BM25**：纯 Python 实现（`rank_bm25` 不引入），中文按字 bigram + 英文按词切分
- **RRF 融合**：`score = Σ 1/(k + rank_i)`，k=60，两路各取 Top-20
- **元数据过滤**：`file_name != doctors.md`（专家名单只走工具），支持按 heading_path 过滤
- **动态 Top-K**：融合分低于阈值时减少注入条数，避免噪声进提示词
- **阈值可配**：融合分阈值 + 向量分阈值双阈值（向量分仍按实测 0.79 标定）
- **可选重排**：用本地模型对 Top-N 重排（可开关，默认关，避免额外延迟）

### 3.5.5 检索可观测与评估

| 项 | 实现 |
|---|---|
| 检索 span | `retrieval.search` span 记录：两路各自的 Top-N 与分数、RRF 融合分、最终注入条数、总耗时、是否降级 |
| 命中/未命中 | 未命中时记录 `no_hit` 并降级为"仅提示词"，不把空知识塞给模型 |
| 召回率评估 | 内置 `tests/eval/rag_eval.json`（问题 → 期望命中的标题路径），pytest 里跑，**改动分块策略后能立刻看出召回有没有掉** |

---

## 四、必须移植的"踩坑修复"（Java 版已验证）

1. **请求体 UTF-8**：Java 版踩过 Spring RestClient 用 ISO-8859-1 把中文变成 `?`；Python 侧显式 `json.dumps(...).encode("utf-8")`
2. **temperature=0.1 / num_ctx=8192**：小模型工具调用稳定性（0.7 时会"用文字代替调用工具"）
3. **空响应自动重试**：Ollama 提示词缓存命中时偶发返回 200 但内容为空
4. **工具 schema 的 `required` 只放必填项**：Java 版漏标 `required=false` 导致模型因"医生姓名"未填而放弃调用工具
5. **Milvus 关逐条 flush**：flush QPS 只有 0.1/s，逐条 flush 被限流拒绝；改为灌库后统一 flush
6. **专家名单不进聊天 RAG**：`file_name != doctors.md` 元数据过滤，专家只走工具，避免模型编造医生姓名
7. **检索阈值 0.79**：实测标定（相关 0.80~0.84，无关 0.66~0.78）
8. **提示词不写"不要提及你调用了工具"**：会让模型把工具调用当纯 JSON 文本输出
9. **专家名单用 ASCII 文件名** `doctors.md`：Java 版踩过 `.properties` 按 Latin-1 读取导致中文文件名乱码

## 五、依赖策略

**原则：只用通用库，不引入任何 Agent 框架。** 所有 Agent 能力（循环 / 工具 / RAG / 记忆）自研。

| 能力 | 选型 | 说明 |
|---|---|---|
| HTTP 服务 | **FastAPI + uvicorn** | 原生 async 流式响应；自动生成 `/docs`（OpenAPI），替代 Java 版的 Knife4j `/doc.html` |
| 出站 HTTP | **httpx** | 调 Ollama `/api/chat`（原生协议，与 Java 版一致，含 tools + stream）；异步流式读取 NDJSON |
| 数据模型 | **pydantic v2** | 对齐 MiniHarness `models/parser.py`、`core/message.py` 的 dataclass 风格 |
| 向量库 | **pymilvus** | 官方 SDK，与 Java 版同样支持维度校验、metadata filter、flush 控制 |
| 关系库 | **mysql-connector-python** | 纯 Python 官方驱动，封装在 `AppointmentRepository` 之后 |
| 文档库 | **pymongo** | 封装在 `ConversationStore` 之后 |
| 配置 | **pydantic-settings** | 对齐 MiniHarness `utils/config.py` 的 `Config` 单例，支持 `.env` |
| 测试 | **pytest + pytest-asyncio** | 对齐参考项目 `pytest.ini` / `conftest.py`，单测用 fake 不打真实服务 |
| 策略配置 | **PyYAML** | 工具权限策略外置为 YAML，支持热加载 |
| 静态检查 | mypy / pylint / black / isort | 版本与参考项目一致 |

> 不用 LangChain 意味着**没有现成的 `EmbeddingStore` / `ContentRetriever` / `ChatMemory`**，
> 这三类抽象需要自己定义（见下节），但实现层直接落到 pymilvus / pymongo，不绕路。

## 六、分阶段实施

| 阶段 | 内容 | 产出 |
|---|---|---|
| P0 | 脚手架：pyproject / config / core 接口 / **tracing 骨架** / HTTP 骨架 | `/health` 200，目录骨架成型 |
| P1 | 模型层：ollama provider、增量解析、质量门控 | 能流式输出纯文本 |
| P2 | 运行时：engine 循环、state、事件流 | 完成"生成→工具→回填→再生成" |
| P3 | 工具层：4 个工具 + SlotService Mock + guardrails | 工具可独立单测 |
| P4 | 记忆 + 检索：ConversationStore、Milvus REST、灌库与检索 | RAG 命中与 Java 版一致 |
| P5 | 服务接入：分块响应、路由、`/doc.html` | **前端零改动跑通全流程** |
| P6 | 可靠性 / 安全 / 追踪可视化页 / 对比脚本 / 测试 / 文档 | pytest 全绿；`/trace` 页面可看到完整流程；README |

## 七、验收标准

- 前端（不改一行）连上 Python 版，可完成：问诊 → 分导诊 → 挂号 → MySQL 落库 → 取消预约
- 同一 `memoryId` 多轮记忆正确
- 查号源 / 查专家返回与 Milvus 中数据完全一致（无幻觉）
- 与 Java 版行为对齐：同一提问的检索命中文本、工具选择、最终落库记录一致
- 单元测试不依赖 Ollama（用 fake），集成测试打本地真实服务
- Java 版继续独立运行，两套可同时起来做 A/B 对比
- **任意一次对话都能在 `/trace` 页面看到从请求到响应的完整步骤、耗时与输入输出**
- `scripts/compare.py` 能对同一问题产出 Java 版 vs Python 版的对比报告

## 八、待确认事项

1. **端口**：Python 版用 8081 与 Java 版并存（便于 A/B 对比），还是顶掉 8080？
2. **知识库**：复制 Java 版这 4 个 md（保持两边数据一致，便于对比），还是 Python 版独立维护一份？
3. **是否需要 MCP**：MiniHarness 含 MCP 集成，硅谷小智目前用不到，建议本期不做。
4. **MongoDB 是否必需**：对话记忆本质是 KV，若想少一个依赖，可只用 MySQL 建一张 `chat_messages` 表承载；建议仍保留 Mongo（与 Java 版一致，便于同库对比）。
