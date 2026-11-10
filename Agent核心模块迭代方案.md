# Agent 核心模块迭代方案

> 范围：消息、记忆、上下文、工具、权限、运行时 loop、结构化输出 —— 七个核心架构模块。
> **不做**：身份鉴权（JWT/登录）、HIS 对接、部署运维（按用户要求暂缓）。
> 基线：commit `2e60c11`，120 单测全绿。目标：本方案全部完成后单测 ≥ 150，且不改变现有前端协议（裸文本流）。

---

## 0. 审计结论总览

| 模块 | 现状 | 最大问题 | 严重度 |
|---|---|---|---|
| 权限 permissions.py | 策略外置 YAML、默认拒绝 | **授权记录了但从不查询（真 bug）**；授权无 TTL；新工具无按风险等级推导的默认策略 | 高 |
| 上下文 context.py | 优先级拼装 + token 预算 | 裁剪按单条切，会把 `assistant(tool_calls)` 和它的 `tool` 结果切开 → 残缺序列 | 高 |
| 运行时 engine.py | 六轮事故磨出的防线很厚 | **落库丢工具结果消息**（下一请求历史残缺）；RetrievalEvent 定义了但从不发 | 高 |
| 工具 tools/ | 归一化 + 注册中心 | 别名表全局硬编码（`arg1` 同时映射 id_card/keyword 冲突）；无 schema 校验（required/type/enum） | 中 |
| 记忆 store.py | 三实现 + 降级包装 | Mongo 异常静默吞掉；append 全量覆盖有竞态；历史无上限增长；降级后永不回归主存储 | 中 |
| 消息 message.py | 统一消息模型 | `from_dict` 一条坏消息崩掉整段历史；`assistant()` 不收 metadata | 中 |
| 输出 event.py/routes.py | 事件体系完整 | 事件只进 trace，SSE 到前端仍是裸文本（兼容约束下无结构化通道） | 中 |

排序原则：先修**正确性 bug**（现在就在产生坏数据），再做**结构性增强**，最后加**结构化输出通道**。

---

## 第 1 批：正确性修复（现在就在产生坏数据）

### T1 权限：授权记录与决策打通【最高优先，真 bug】
- `permissions.py::decide()` 里 `ASK` 分支**从不调用** `_was_approved_before()` —— `record_approval` 记录的授权是死数据，写工具每次都要走 no_ui 兜底。
- 修复：ASK 且 `(user_id, tool_name, 参数指纹)` 已授权 → 直接 ALLOW。
- 授权加 **TTL**（默认 300s，YAML 可配 `approval_ttl_seconds`），过期自动失效；线程安全。
- `no_ui_policy` 自由字符串 → 限定 `allow|deny`，非法值启动即报错。
- 测试：授权后同参数二次调用放行、不同参数仍需确认、TTL 过期重新确认。

### T2 上下文：原子块裁剪 + 完整 token 计量
- 现状：`assemble()` 超预算时按单条消息从头部 pop —— 会把 `assistant(tool_calls)` 与其后紧跟的 `tool` 结果切开，发给 Ollama 的序列对不上 `tool_call_id`。
- 修复：裁剪以**原子块**为单位（块 = user 消息，或 assistant+其全部 tool 结果），宁可少保留也不切半块。
- token 计量补全：`tool_calls` 参数 JSON 与 tool 结果正文当前不计入预算（只算 `content`）→ 实际注入超预算。统一按完整消息估算。
- 测试：构造含工具轮的历史，验证裁剪后序列完整（每个 tool_call 都有配对结果）。

### T3 运行时：落库补全工具结果消息
- 现状：`memory.write` 只存 `user + assistant(text, tool_calls)`，**工具结果消息丢失** —— 下一请求从历史里看到"assistant 发起过 tool_calls"却没有对应结果，模型可能据此脑补结果（与历史中性化机制叠加时有歧义）。
- 修复：本轮发生工具调用时，把 `tool` 结果消息一并按顺序落库。
- 注意：与 `booking_executed_in_history`（读 metadata 标记）兼容，不改变判定逻辑。
- 测试：FakeStore 断言落库消息序列完整。

### T4 消息：反序列化容错
- 现状：`Message.from_dict` 里 `Role(data["role"])` 遇到未知 role 直接 ValueError —— Mongo 里一条脏数据会让**整个会话历史加载失败**（load 返回 []，等于失忆）。
- 修复：新增 `from_dict_safe()`（非法 role/timestamp/结构 → 返回 None + 原因），三个 Store 的 `load` 改用安全解码并跳过坏消息，trace 里记 `skipped_invalid` 计数。
- 测试：混入脏消息后加载不崩、坏消息被跳过。

---

## 第 2 批：结构性增强

### T5 工具：schema 驱动的参数归一化
- 现状：`argument_normalizer.py` 是全局硬编码别名表，且 `arg1` 同时映射 `id_card` 和 `keyword`（冲突，先到先得）；换个新工具就要改这张表。
- 修复：新增 `normalize_arguments_by_schema(args, definition)` —— 从 `ToolDefinition` 的参数**自动生成别名**（参数名、snake↔camel、参数中文 description 全文匹配），旧表降级为无 schema 信息时的兜底。`SecureToolExecutor` 在 L2 前调用归一化，工具内部逐步迁移。
- 测试：同一入参用中文描述名/驼峰/位置参数三种形态都能归位。

### T6 工具：schema 校验接入安全执行器 L2
- 现状：L2 只做"必须是字符串"的通用体检；required 缺失、enum 越界、类型不对全靠各工具自己写（写漏了就漏）。
- 修复：新增 `validate_arguments(args, definition)`（required/type/enum 三检），executor 统一调用；错误文案带纠正指引（沿用第六轮事故的"缺时段 → 反问时段"模式）。
- 注意归一化在后、校验在前（先归位再检查），顺序不能反。

### T7 权限：按风险等级推导默认策略
- 现状：未在 YAML 登记的工具默认 DENY —— 默认拒绝是对的，但每加一个只读工具都要手动改 YAML。
- 修复：`policy_of` 未命中时按工具 `risk_level` 推导：`READ_ONLY→auto`、`WRITE→ask`、`DESTRUCTIVE→deny`；YAML 显式配置永远优先。决策引擎增加 `register_tool_default(tool_name, risk_level)` 由装配层在注册工具时调用。
- 测试：未登记的只读工具放行、未登记的写工具要求确认、YAML 显式 deny 覆盖推导。

### T8 记忆：存储层健壮化
- Mongo `save/clear` 异常 `except: pass` 静默吞掉 → 改为 logger.warning + `degraded_count` 计数（可观测）。
- `append` 是 `load(10000)+save` 全量覆盖：O(n) 且同会话并发请求会互相覆盖丢消息 → 引入**每 memory_id 异步锁**串行化写（基类提供 `locked(memory_id)` 上下文管理器）。
- 历史无上限增长 → save 时按 `max_messages`（默认 200）截断保留最新。
- `ResilientConversationStore` 降级后永不回归主存储 → 冷却期（默认 30s）后自动重试主存储，成功即恢复。
- `FileConversationStore` 非原子写（进程崩溃会写坏 JSON）→ 写临时文件 + `os.replace` 原子替换。
- 测试：并发 append 不丢消息、降级恢复、坏文件加载不崩。

### T9 消息：构造器补全 + 序列约束校验
- `Message.assistant()` 增加 `**metadata` 透传（engine 现在只能绕过构造器直接 `Message(role=...)`）。
- 新增 `validate()`：assistant 带 tool_calls 必须有 id/name；tool 消息必须有 tool_call_id —— T3 落库前调用一次，防脏数据入库。

---

## 第 3 批：结构化输出与事件补全

### T10 结构化 SSE 通道（前端零改动）
- 现状：事件体系完整（10 类事件 + to_json），但只进 trace；`/xiaozhi/chat` 到前端是裸文本（兼容约束不能动）。
- 修复：`/xiaozhi/chat?format=events` 返回 SSE —— 每个事件一行 `data: {event.to_json()}\n\n`；默认（无参数）保持裸文本完全不变。新前端/调试工具/未来的流式 UI 走 events 通道。
- `TEXT_RESPONSE` 事件在 events 模式下带 `delta`，其他事件带各自 metadata（tool_name、tool_id、turn、degraded 原因…），一次请求即可在客户端还原完整执行过程。

### T11 运行时：事件补全
- 知识注入时 `yield RetrievalEvent`（含 hit_count / 首条 heading_path）—— 事件定义了但 engine 从不发，结构化通道里看不到检索步骤。
- `ToolResultEvent.metadata` 补 `result_preview`（截断 160 字）与 `duration_ms` —— 现在只有内容长度，结构化消费者无法判断工具成败细节。
- 纠正重试 / 幻觉截断 / 兜底文案等关键节点补发事件（目前只进 trace 与 metrics），让 events 通道成为"完整的执行过程视图"。

### T12 上下文：长历史压缩钩子（接口先行）
- `ContextAssembler` 增加 `summarizer` 可选依赖：历史超预算裁剪前，若装配了 summarizer 则把被裁掉的最老 N 条压缩成一条 summary 消息塞回窗口头部（保留更早的上下文）。
- 本批只定义接口 + 离线实现（规则摘要：保留操作类消息与工具结论，压缩寒暄），不接 LLM 摘要；接口留好，后续可换 LLM 实现。

---

## 执行顺序与回归

| 批次 | 任务 | 预计新增测试 | 完成标志 |
|---|---|---|---|
| 第 1 批 | T1 → T2 → T3 → T4 | ~14 | 全绿 + 复现验证授权放行/裁剪完整性 |
| 第 2 批 | T5 → T6 → T7 → T8 → T9 | ~18 | 全绿 + Mock 工具走完四层安全流 |
| 第 3 批 | T10 → T11 → T12 | ~8 | 全绿 + `?format=events` 手工验证 |

- 每批完成后跑 `.venv/bin/python -m pytest tests/unit/ -q` 全量回归（当前 120 基线）；
- 第 1/2 批完成后用真实后端（8081）验证一次写工具的"授权→放行"链路与结构化 SSE；
- 全部完成后统一提交推送（每批一个 commit，方便回滚）。

## 明确不做（本轮）
- 身份鉴权（JWT / 登录 / 会话归属校验）—— 按要求暂缓；
- engine.py 832 行大重构 —— 风险高、防线逻辑刚经六轮事故验证，只做增量；
- 长期记忆向量库 / 跨会话画像 —— 属记忆模块二期，等 T8/T12 落地后再评估。
