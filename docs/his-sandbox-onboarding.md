# HIS 模拟沙箱与接入契约（Phase 3b）

> **本项目定位（2026-11-12 明确）**：这是**本地学习/迭代项目，不做真实上线**。
> 目标是**尽量真实地模拟医院 HIS**，让「预约挂号 / 取消挂号 / 查询科室 /
> 查询医生 / 查询号源」这些工具接口都从 HIS 取数、行为贴近真实医院；
> **将来若真接院方系统，改动必须集中在适配层，别的业务代码不动**。
>
> 因此**验收标准不是「连过真实医院」**，而是两条：
> ① 模拟保真度（院方数据单一源、字典/排班/号源/挂号单同源、边界行为齐全）；
> ② 适配层集中度（接真 HIS 时只改少数几个文件）。见第 0 节与第 0.5 节。
>
> 下文 1~4 节记录的是**未来若要真接院方系统**才需要的外部条件，属于
> 「备查清单」，不是当前迭代的阻断项，也不代表本项目在等待真实对接。
> `IMPLEMENTED_REAL_SLOT_ADAPTERS` 仍为空（production 刻意拒绝启动），
> 这是安全门，不是缺口。

---

## 0. 本地沙箱验收结论（已完成，2026-11-12）

参考项目（Spring Cloud HIS）缺业务 DDL、缺 Nacos 配置、缺独立预占/退号，
且被 `.gitignore` 排除，因此在主后端新增自包含沙箱
`guiguXiaozhi_backend_py/his_sandbox/`，不改动任何第三方代码。

**7 类接口全部真机通过**（`scripts/his_full_flow_check.py`，10/10）：

| # | 接口 | 本地沙箱 |
|---|---|---|
| 1 | 号源查询 `GET /clinicForRegist/list` | OK |
| 2 | 预占 `POST /clinicForRegist/hold` | OK（参考项目缺失，沙箱补齐） |
| 3 | 落单 `POST /currentRegister/add` | OK |
| 4 | 退号 `POST /currentRegister/cancel` | OK（参考项目缺失，沙箱补齐） |
| 5 | 查单 `GET /currentRegister/{id}` | OK |
| 6 | 患者主档 `POST /patMasterIndex/add` + `GET /patMasterIndex/list` | OK |
| 7 | 科室字典 `GET /dept/list` | OK |
| 8 | 医生名录 `GET /doctor/list`（含职称/擅长） | OK（本次新增，供「查询科室专家」工具） |

另有 `GET /doctor/list`（医生名录：职称/擅长）、`GET /currentRegister/byKey/{key}`
（按幂等键查单，解 UNKNOWN 对账死结）、`GET /clinicMaster/list`（挂号单列表）。

**院方数据只有一个源（2026-11-12 架构收敛）**：科室字典、医生名录、排班、
号源、挂号单、患者主档全部由 **HIS 自持**（`sys_dept` / `sys_doctor` /
`clinic_index` → `clinic_schedule` → `clinic_for_regist` / `clinic_master` /
`pat_master_index`）。应用侧**没有任何第二份院方数据**：

- 「查询科室 / 查询医生」→ `HisDoctorService` 读 HIS 的 `/dept/list`、
  `/doctor/list`（不再从知识库 `doctors.md` / Milvus 解析）；
- 工具描述里的权威科室清单、系统提示词第 9 条、姓名错位校验，
  全部走同一个 `DoctorService` 抽象 → 也就走 HIS；
- 「预约 / 取消 / 查号源」→ `HisSlotService` 读 HIS；
- 落单用的患者引用 → 落单前先向 HIS 建档，用 **HIS 返回的 `patientRef`**，
  不再拿本地 `sha256(salt|idNo)` 派生值硬塞（真机复现过「患者主档不存在」）。

`doctors.md` 现在只是 **HIS 的院方导入源文件**（HIS 启动时一次性灌进主档表），
应用侧运行时不读它。这样「查得到的医生」与「约得到的号」永远同源。

**全流程正确姿势**：建档 → **用 HIS 返回的 `patientRef`** → 预占 → 落单 →
查单 → 退号。本地派生的 `patient_ref` 直接落单会被拒「患者主档不存在」，
这是引用体系不一致（见 `ops-runbook.md` 11.7），不是接口缺失。

**单一源真机核验**：`scripts/his_single_source_check.py` —— 用**真实工具对象**
跑完整链路（查科室 → 查医生 → 查号源 → 预约 → 查单 → 取消），同时验
「字典与号源同源」「患者引用由 HIS 签发」「端点映射集中」。
两套 HIS 实现（内嵌替身 + Docker MySQL 沙箱）合计 **26/26 通过**。
脚本自包含：内部临时起服务、跑完自动关闭，不留后台进程。

**边界演练**：`scripts/his_fault_drill.py`（沙箱 19 条）+
`scripts/local_his_fault_drill.py`（本地替身 19 条），合计 38 条真机边界用例
全过；七类接口全流程 `scripts/his_full_flow_check.py` 10/10；
全量单测 **763 passed / 6 skipped**。分类纪律与处置口径见
`ops-runbook.md` 第 11 章。

**已闭合**：`AppointmentService` 落单改用 HIS 建档返回的 `patientRef`（原先
runbook 11.6/11.7 记的 P0-06 未闭合项已收敛）。PII 密文与建档的边界：
建档发生在**落库之前**，此时 `id_card` 还是入参明文；库里的密文只在
查重/回显链路出现，两者不交叉。

---

## 0.5 接真实 HIS 时「唯一改动点」（改动集中契约）

这是本项目最重要的架构约束：**换掉 HIS 实现时，工具层、业务层、
提示词、领域模型一律不动**。需要改的只有下面这些（按优先级）：

| # | 文件 / 配置 | 改什么 | 是否必须 |
|---|---|---|---|
| 1 | `.env`：`SLOT_SERVICE_IMPL=his`、`HIS_API_BASE_URL`、`HIS_API_TOKEN`、`HIS_ORG_ID` | 网关地址与认证 | 必须 |
| 2 | `.env`：`HIS_*_PATH`（14 个端点） | 院方路由与参考项目不同时覆盖 | 视院方 |
| 3 | `xiaozhi/service/his_client.py` → `_paths` 默认值 | 若不想用环境变量，直接改这张表 | 二选一 |
| 4 | `xiaozhi/service/his_client.py` → `_to_slot()` / 字典字段映射；`his_doctor_service.py` → `_FIELD_MAP` | 院方字段名不同（如 `empName` / `professionalTitle`）时吸收差异 | 视院方 |
| 5 | `xiaozhi/service/his_resilience.py` → 重试 / 熔断 / 超时参数 | 院方 SLA 不同 | 视院方 |
| 6 | `config_guard.IMPLEMENTED_REAL_SLOT_ADAPTERS` | **只有端到端验收全过后**才登记 | 上线才需要 |

**明确不需要改**：`tools/appointment_tools.py`（4 个工具）、
`service/appointment_service.py`（预约状态机）、`service/his_reconcile.py`、
`application.py::load_system_prompt()`、`repositories/`、前端。
它们只依赖 `SlotService` / `DoctorService` 两个抽象和 `HisClient` 的返回值，
院方差异被适配层吃掉了。

**切换后的自检**：跑 `scripts/his_single_source_check.py --sandbox <网关>`，
26 条全部通过即说明适配层契约对齐；任何一条挂了都不要改工具层去"迁就"，
应回到上表 1~5 改适配层。

---

## 一、必须向医院/业务方索取的东西

### 1. 接口文档（缺一不可）

| # | 需要什么 | 为什么必须有 | 当前状态 |
|---|---|---|---|
| 1.1 | **号源查询接口**（查询某科室某日某时段的号表实例） | 没有它就无法回答"还有没有号" | 参考项目推断为 `GET /clinicForRegist/list`，**未验证** |
| 1.2 | **预占（锁定）号源接口** | 审计 P0-03：「查询有号 ≠ 实际锁定号源」。没有独立预占就无法防并发超卖 | 参考项目**没有独立预占端点**（它是"先扣号源再落单"的组合动作）——需要医院确认是否可拆分 |
| 1.3 | **挂号落单接口** | 真正产生挂号单 | 参考项目推断为 `POST /currentRegister/add`，**未验证** |
| 1.4 | **退号/取消接口** | 取消必须同步到 HIS，否则本地说取消了、医院还留着 | **完全未知** —— 参考项目未调研 |
| 1.5 | **查单接口**（按单号查挂号状态） | **对账的唯一依据**：UNKNOWN 记录靠它收敛 | 参考项目推断为 `GET /currentRegister/{id}`，**未验证** |
| 1.6 | **患者主档查询/建档接口** | 挂号单要挂到患者档案上；没有患者主档就无法落单 | 参考项目有 `PatMasterIndex` 患者主档表，接口形态**未调研** |
| 1.7 | **科室/专家字典接口** | 我们的科室名与 HIS 科室编码的映射 | 本地沙箱已有 `/dept/list` + `/doctor/list`；应用侧通过 `HisDoctorService` 读取，**不再从知识库解析**。真接院方时只需对齐字段名（见 0.5 节） |

### 2. 认证方式

| # | 需要什么 | 备注 |
|---|---|---|
| 2.1 | 认证方式（token / mTLS / 签名） | 参考项目用 **Sa-Token**（请求头 `satoken`），但**外网暴露的网关**通常另有鉴权层 |
| 2.2 | token 获取与刷新流程 | 沙箱 token 有效期、是否需要续期 |
| 2.3 | IP 白名单 / VPN 要求 | 医院侧通常只允许内网或固定出口 IP |

### 3. 幂等语义（**最容易被忽略、后果最严重**）

| # | 需要什么 | 为什么 |
|---|---|---|
| 3.1 | HIS 侧是否支持幂等键？键放在哪（头 / 请求体 / 无）？ | **没有幂等语义 = 网络抖动就会重复挂号**。我们已经在请求头带 `X-Idempotency-Key`，需要医院确认是否识别 |
| 3.2 | 幂等键的**保留时长** | 短于我们重试窗口就不够 |
| 3.3 | 相同幂等键但**参数不同**时 HIS 的行为（报错还是覆盖） | 我们用同一业务键派生幂等键，参数变了必须被 HIS 拒绝 |

### 4. 预占语义

| # | 需要什么 | 为什么 |
|---|---|---|
| 4.1 | 预占的有效期（秒） | 预占过期后号源自动释放，我们需要知道多久算过期 |
| 4.2 | 预占是否需要显式释放 | 失败回滚路径依赖它 |
| 4.3 | 号源的**并发保护**在 HIS 侧如何实现（乐观锁？行锁？） | 决定我们能不能依赖 HIS 侧防超卖 |

### 5. 沙箱环境

| # | 需要什么 |
|---|---|
| 5.1 | 沙箱 base_url + 访问方式 |
| 5.2 | 测试账号 / 测试患者主档（构造若干条已知状态的挂号单，用于对账验证） |
| 5.3 | 可造的数据（能造出"号源已满""患者不存在""预占过期"等异常场景吗） |
| 5.4 | HIS 侧的日志/查询手段（对账出问题时我们需要能查 HIS 侧发生了什么） |

---

## 二、拿到之后我们要做的（对接步骤）

```
1. 改配置（不动代码）：
   SLOT_SERVICE_IMPL=his
   HIS_API_BASE_URL=<网关地址>
   HIS_API_TOKEN=<token>
   HIS_ORG_ID=<机构号>
   HIS_*_PATH=...   （14 个端点，见 config.py / 本文 0.5 节）
   HIS_PATIENT_REF_SALT=<部署环境注入，不要进仓库>

2. 对齐端点：真实路径与默认值不同 → 改配置或改 HisClient._paths 默认值

3. 字段映射核对：HIS 返回的科室名/时段枚举/医生字段与我们的取值是否一致
   （我们传 "上午"/"下午"，HIS 可能用 "AM"/"PM" 或数字；
    医生姓名可能是 empName，职称可能是 professionalTitle → 改 _FIELD_MAP）

4. 端到端验收（必须逐条留证据）：
   □ 号源查询：剩余号与 HIS 后台一致
   □ 正常预约：HIS 侧确实产生挂号单，本地 CONFIRMED
   □ 并发同参数 20 次：HIS 侧只有 1 单（幂等 + 预占生效）
   □ 预占后落单失败：号源被释放，未产生挂号单
   □ 模拟 HIS 超时：本地 UNKNOWN + 固定文案，用户不会被误导
   □ 对账 Job：UNKNOWN 记录能被收敛为 CONFIRMED / FAILED
   □ 取消预约：HIS 侧同步退号

5. 验收全过 → 才把 "his" 加进 config_guard.IMPLEMENTED_REAL_SLOT_ADAPTERS
   （production 门禁放行的唯一依据）

6. 同步更新：本文件状态、差异清单 P0-03 状态、路线图 Phase 3b 勾选
```

---

## 三、我们已经做了什么（不需要医院配合的部分）

| 组件 | 位置 | 状态 |
|---|---|---|
| HIS REST 客户端（统一响应包 `R{code,msg,data}` 解包、Sa-Token 认证、错误分类） | `service/his_client.py` | ✅ 完成，22 条契约测试 |
| 号源适配器（查询/预占/确认/释放/查单） | `service/his_slot_service.py` | ✅ 完成 |
| 两段式预约流程（预占→落库 HOLDING→确认→CONFIRMED） | `service/appointment_service.py::_book_with_hold` | ✅ 完成 |
| UNKNOWN 对账 Job | `service/his_reconcile.py` | ✅ 完成 |
| 预约表 HIS 字段（外部单号/预占号/患者引用/对账计数） | 迁移 004 | ✅ 完成 |
| 组合根装配（`slot_service_impl=his`） | `application.py::_build_slot_service` | ✅ 完成 |
| 契约/混沌测试（幂等并发、超时=不确定、对账收敛、预占释放） | `tests/unit/test_his_integration_contract.py` | ✅ 22 条 |

**关键设计（不依赖医院实现，可以先定）**：

1. **超时 = 不确定（UNKNOWN），不是失败** —— 否则用户重试会重复挂号。
2. **HTTP 200 ≠ 业务成功** —— 必须看 `code`。
3. **剩余号必须扣预占数**（`limitNum - currentNum - currentPreNum`）。
4. **幂等键是 sha256**，不把身份证号原样送到 HIS（会进 HIS 侧日志）。
5. **预占失败必须释放** —— 否则号源被白占。
6. **对账只认外部单号** —— 没有单号就无据可查，保持 UNKNOWN 不猜。

---

## 四、当前风险提示

| 风险 | 影响 | 缓解 |
|---|---|---|
| HIS 侧**没有幂等语义** | 网络抖动 → 重复挂号 | 上线前必须确认；没有的话我们要在 HIS 侧加一层幂等表（或走 HIS 的挂号唯一约束） |
| 预占与落单**无法拆分**（参考项目就是组合动作） | 失败回滚路径不成立（扣了号源但没落单 → 号被占死） | 需要 HIS 提供释放接口，或接受"由对账 Job 定期清理僵尸预占" |
| 科室/时段**枚举不一致** | 查不到号 → 用户看到"暂无出诊安排" | 沙箱阶段做一次全科室枚举对齐 |
| 真实 HIS 的响应延迟高于我们 10s 超时 | 频繁 UNKNOWN，用户体验差 | 沙箱阶段测 P99 延迟，按实测调 `HIS_API_TIMEOUT` |
| **上线宣称风险** | 适配层写完 ≠ 真实挂号可用 | `IMPLEMENTED_REAL_SLOT_ADAPTERS` 保持为空，production 门禁继续拒绝启动（已有测试钉死） |
