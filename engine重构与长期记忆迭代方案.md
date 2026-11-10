# Engine 重构与长期记忆迭代方案（二期）

> 范围：① `runtime/engine.py` 大重构（行为零变更）；② 长期记忆向量库 / 跨会话画像。
> **不做**：身份鉴权（JWT/登录）、HIS 对接、部署运维——与一期一致，继续暂缓。
> 基线：commit `4645ca9`，189 单测全绿（第七~第十轮事故修复已含在内）。
> 目标：P1 完成后测试 ≥ 205 且行为等价；P2 完成后测试 ≥ 235，前端协议（裸文本流）始终不变。

---

## 总原则

1. **P1 先行**：engine 是十轮事故磨出的防线所在地，先重构地基、再在干净的地基上盖长期记忆的楼（P2 的"会话结束钩子"要挂在重构后的循环上）。
2. **P1 是行为零变更重构**：189 个现有测试一个不许改语义；动手前先补**特征测试**（characterization tests）把现行为钉死。
3. **每批一个 commit，可独立回滚**；每批完成跑全量 `.venv/bin/python -m pytest tests/ -q`。
4. **P2 灰度开关**：`profile_enabled` 默认 `false`，全部实现离线可测，打开开关才生效。

---

# 第一部分：engine.py 大重构（P1）

## 0. 审计现状（为什么现在重构）

`runtime/engine.py` 已膨胀到 **918 行**（一期方案审计时 832 行，之后第七~第十轮事故又加了 86 行）：

| 问题 | 现状 | 后果 |
|---|---|---|
| 跨轮状态散落 | `result.text`、`narrative_text`、`final_reply_text`、`corrective_pending/kind/used`、`truncated_claim`、`suppressed`、`turn_buffer`、`tool_failures`、`last_tool_failure`、`persisted_tool_results`… **14+ 个闭包变量** | 第十轮"纠正重试拼接"事故的直接根源：状态没有单一事实来源 |
| 质量防线内联 | claim 截断、narrated 门控、false_confirmation、query 门控、纠正调度散落在 340-670 行的嵌套 if 里 | 每轮事故都在同一个函数里加分支（第九轮加了 is_greeting，第十轮改 final_reply_text），改一处怕碰三处 |
| 流式过滤内嵌 | 回看窗口、括号配对切割、textual JSON 截断与 yield 业务耦合 | 只能端到端测，chunk 边界 bug（中文跨 chunk）难以单测复现 |
| 落库逻辑内嵌 | 三段式落库（第七轮）+ final_reply_text 修正（第十轮）直接写在 run() 尾部 | 落库结构已经改过两轮，没有独立测试单元 |

**重构目标**：run() 缩到 ~300 行纯编排，防线全部变成可独立测试的组件。**不改变任何可观测行为**（事件序列、落库结构、SSE 输出、日志语义）。

## E1 特征测试补齐（重构前置，~8 个测试）

现有 189 个测试覆盖了大部分防线，先把缺口钉死——重构后这些测试**一个字不改**就是行为等价的证明：

- 流式回看窗口：承诺词跨 chunk 到达时截断点正确（构造逐字 delta 序列）
- textual JSON 截断：`_trim_to_sentence` 切割点在句子边界
- 纠正重试只做一次：轮 2 仍编造时走兜底文案而非二次重试
- 时间预算触发：`request_timeout` 到期先补发回看窗口内容再降级
- 指标埋点齐全：`narrated_tool_call_retries / hallucination_fallbacks / false_confirmation_truncated` 在对应场景自增
- 落库快照：一次含"叙述轮+工具轮+最终轮"的请求，落库 JSON 结构整体断言（不只逐字段）

## E2 TurnState 状态收敛

- 新增 `runtime/state.py`：`TurnState` 数据类收敛全部跨轮变量，提供 `append_final(text)`、`append_narration(text)`、`schedule_corrective(kind)` 等方法，**方法内部维护不变量**（如：`final_reply_text` 永远是最后一轮无 tool_calls 的文本；叙述段永不混入 final）。
- 第十轮"落库用 final_reply_text 而非 result.text"这类修复，以后变成 state 方法的一行改动 + 一个单测。
- 完成标志：`run()` 内不再有裸变量赋值这些状态，全部经 `state.` 访问；grep 不到 `corrective_pending =` 之类的散装赋值。

## E3 StreamFilter 独立

- 新增 `runtime/stream_filter.py`：`StreamFilter(quality_ctx)` —— `feed(delta) -> Optional[str]`（None=本段被拦截）+ `flush() -> str`（流结束时补发回看窗口余量）。
- 迁入：回看窗口（LOOKBEHIND）、claim_span 拦截、textual_tool_call_span 切割、`_safe_prefix` / `_trim_to_sentence`。
- engine 里的 `async for delta` 循环瘦成：`safe = filter.feed(delta); if safe: yield`。
- **关键测试**（现在写不出的）：中文承诺词按 1-2 字粒度跨 chunk 撕开时，拦截点与第十轮联调行为一致；`flush()` 在流异常时也补发。

## E4 质量防线插件化（拦截器链）

- 新增 `runtime/interceptors.py`：`TurnInterceptor` 协议 + 有序链。
  - 协议：`inspect(turn: ChatTurn, state: TurnState) -> InterceptorDecision`；Decision 为 `{pass | truncate(claim) | schedule_retry(kind) | replace_with_fallback(text)}`。
  - 实现四个拦截器（从现有代码平移，逻辑一行不改）：
    1. `FalseConfirmationInterceptor`（claim_span / false_confirmation 检测与 query 门控）
    2. `NarratedToolCallInterceptor`（叙述承诺 → 纠正调度，仅 operation_request）
    3. `TextualToolCallInterceptor`（纯文本 JSON 工具调用）
    4. `CorrectiveRetryScheduler`（消费前三个的 Decision，维护 corrective_used ≤ 1 不变量）
- intent 判定（is_operation_request / should_inject_knowledge）**保持独立模块**，拦截器只读结果。
- 完成标志：engine 里不再出现 `_NARRATED_TOOL_CALL`、`claim_span` 等具体检测的调用点；新增一种换皮句式只改 `models/quality.py` 的正则，engine 零改动（这条本来就是现状，但重构后要用测试钉住"engine 不 import 具体正则"）。

## E5 ConversationPersister 独立

- 新增 `runtime/persister.py`：输入 `(history, user_input, state, tool_results)`，输出落库消息列表。三段式结构（叙述/工具结果/最终回复）、`Message.validate()`、outcome_meta 组装全部迁入。
- FakeStore 断言从"引擎端到端"变成 persister 单测（快、准、可枚举边界）。
- 兼容：`booking_executed_in_history` 读 metadata 的逻辑不动。

## E6 文件拆分收尾

```
runtime/
  engine.py          # ~300 行：run() 主循环编排
  state.py           # TurnState
  stream_filter.py   # 流式过滤
  interceptors.py    # 质量防线链
  persister.py       # 落库组装
```

- 事件类型、metrics 埋点点位不变（events 通道消费者无感知）。
- 完成标志：`wc -l engine.py ≤ 350`；全量测试 ≥ 205 全绿；真实后端 8081 冒烟（预约全流程 + `?format=events` 事件序列与重构前一致——重构前先录一份事件序列做对照）。

**P1 预计新增测试 ~25**，分 E1(8) / E3(6) / E4(6) / E5(5)。

---

# 第二部分：长期记忆向量库 / 跨会话画像（P2）

## 0. 现状边界与目标

- 现在只有**会话内记忆**：Mongo `chat_memory_db`（一期 T8 健壮化过）。会话结束即失忆，患者每次都要重报身份证号、过敏史、偏好。
- 知识库 Milvus alias = `xiaozhi_knowledge_py`（与 Java 版隔离）；embedding 模型 `andersc/qwen3-embedding:0.6b`；数据层模式：`repositories/` + `service/`。
- **目标**：记住"这个人"——过敏史、慢性病、常挂科室、偏好医生/时段、家庭医生咨询记录，跨会话注入，让老患者不用重头再来。

## M1 事实抽取器 ProfileExtractor（~6 个测试）

- 触发：会话内每 6 轮 + 会话结束（engine 收口处挂钩子，P1 重构后这是一行注册）。
- 两路抽取：
  - **规则路**（确定性，零风险）：姓名/身份证号（已有正则）、日期时段偏好、科室名（对照权威名单）；
  - **LLM 路**（qwen2.5:7b）：过敏史/慢性病/症状偏好，输出必须过 **JSON schema 门控**（`{"facts": [{"key", "value", "confidence"}]}`，解析失败/字段越界 → 整批丢弃，宁缺勿错）。
- 产出 `FactEntry{key, value, source_trace_id, ts, confidence}`；`confidence < 0.7` 丢弃。
- 抽取用独立小 prompt，**与主对话隔离**，失败只记日志不影响主流程。

## M2 结构化画像存储（~5 个测试）

- MySQL 新表 `user_profile`（库 `guiguxiaozhi`）：`user_key, fact_key, fact_value, confidence, source_trace_id, updated_at`，唯一键 `(user_key, fact_key)`。
- **身份证号等敏感字段对称加密存储**（Fernet，密钥走 env `PROFILE_SECRET_KEY`，启动缺失则画像模块整体禁用并告警）。
- `repositories/profile_repo.py`：upsert（新事实覆盖旧事实，保留 updated_at 历史）；查询按 key 精确取。
- `user_key` 生成：`sha256(id_card)[:16]`（身份证号是最稳的天然主键；无身份证号会话不建画像）。

## M3 向量画像记忆（~4 个测试）

- Milvus **新集合** `xiaozhi_profile_facts`，**独立 alias** `xiaozhi_profile_py`——绝不与知识库 `xiaozhi_knowledge_py` 混用（隔离是本项目铁律）。
- 只存**模糊描述类**事实（"对青霉素过敏，幼时服药起疹""偏好上午复诊"），embed 后带 `user_key` 标量分区。
- 检索：当前对话语义 top-3 + `user_key` 过滤；精确字段（身份证/姓名）**只走 MySQL**，不进向量库——精确的事精确查，模糊的事向量找。

## M4 注入路径（~4 个测试）

- `ContextAssembler` 增加画像段，token 预算 **≤ 150**，优先级：system > 当前请求历史 > **画像段** > RAG 知识。
- 注入形态（沿用 RAG 教训——不用"请参考知识回答"措辞，避免压掉工具调用率）：

  ```
  [患者档案] 王先生（已实名），青霉素过敏史；近半年常挂呼吸内科；偏好上午时段。
  ```

- 画像段**只陈述事实，不指挥行为**；operation_request 请求照常不注入 RAG，但画像段保留（挂号时过敏史恰恰最有用）。
- 时效：画像内容超过 180 天且未被本次会话引用的事实不再注入。

## M5 遗忘与合规红线（~3 个测试）

- 覆盖式更新即天然遗忘（新事实顶旧事实）；`DELETE /profile/{user_key}`（events 通道同源新增，供未来前端接"忘记我"）。
- **红线（写成测试）**：
  1. 画像**绝不参与**重复挂号判定——`find_duplicate` 只认 MySQL 预约表，两者职责永不清洗；
  2. 画像注入文本永不包含完整身份证号（脱敏为 `6101…2831`）；
  3. 抽取失败/开关关闭时，主对话链路行为与现在**完全一致**（回归测试：`profile_enabled=false` 时全部 189+ 测试原样通过）。

## M6 灰度开关与真实流量验证

- `settings.profile_enabled: bool = False` + `profile_extract_interval_turns: int = 6`；装配层按开关决定是否注册钩子与注入段。
- 完成标志：开关关闭全量测试绿；打开后真实验证——① 首会话说"我对青霉素过敏"→ 新会话直接问"帮我预约输液"→ 回复体现过敏事实；② Mongo/Milvus/MySQL 三处数据落点正确；③ 敏感字段密文落库。

**P2 预计新增测试 ~22**（M1:6 / M2:5 / M3:4 / M4:4 / M5:3）。

---

## 执行顺序与回归

| 阶段 | 任务 | 预计新增测试 | 完成标志 |
|---|---|---|---|
| P1-E1 | 特征测试补齐 | ~8 | 全绿，防线行为快照固化 |
| P1-E2 | TurnState 收敛 | 0（既有测试守恒） | run() 无散装状态变量 |
| P1-E3 | StreamFilter 独立 | ~6 | 跨 chunk 边界用例可单测 |
| P1-E4 | 拦截器链 | ~6 | engine 不 import 具体检测正则 |
| P1-E5/E6 | Persister + 拆分 | ~5 | engine ≤ 350 行，事件序列对照一致 |
| P2-M1 | 事实抽取器 | ~6 | JSON 门控失败整批丢弃 |
| P2-M2/M3 | MySQL 画像 + Milvus 集合 | ~9 | 集合隔离、密文落库 |
| P2-M4/M5 | 注入路径 + 合规红线 | ~7 | 三条红线测试钉死 |
| P2-M6 | 灰度验证 | — | 开关关闭行为等价，打开端到端生效 |

- 每批完成：全量 `pytest tests/ -q` + 提交推送；
- P1 结束：真实后端冒烟 + 事件序列对照；
- P2 结束：开关双态真实流量验证。

## 明确不做（本轮）

- 身份鉴权：`user_key` 先用身份证号派生，一台设备/一个身份证一个画像；未来接登录后无缝换真 user_id；
- 多用户并发画像冲突细化（单机演示场景）；
- LLM 摘要替换 rule_based_summary（P2 只留接口，同一期 T12 的做法）；
- 画像的跨设备同步、导出（等鉴权上线再说）。
