# 本地预约挂号全流程可用指南

> 目标：**不连任何医院系统**，本地就能把预约挂号涉及的工具接口真跑一遍 ——
> 查号源、预约、取消、查专家，而且**号源是真的会被扣掉和归还的**。

---

## 一、一行配置启动

在 `guiguXiaozhi_backend_py/.env` 里加：

```bash
# 号源适配器：local_his = 本地 HIS 替身（行为完整，零外部进程）
SLOT_SERVICE_IMPL=local_his
# 本地 HIS 状态持久化（号源计数、挂号单；留空则只在内存里，重启丢失）
LOCAL_HIS_STATE_PATH=../logs/local_his_state.json
# 患者引用脱敏盐（生产必须换成随机值；本地固定值方便复现）
HIS_PATIENT_REF_SALT=local-dev-salt
```

然后照常启动：

```bash
cd guiguXiaozhi_backend_py && ./.venv/bin/python run.py
```

启动日志会出现：

```
号源适配器：本地 HIS 替身（http://127.0.0.1:54321，排班 12 条，限号 5）
```

**不需要**额外进程、不需要医院环境、不需要改任何业务代码。

---

## 二、它与 Mock 号源的区别（为什么值得这么用）

| | `mock`（旧） | `local_his`（新） |
|---|---|---|
| 号源怎么来的 | 医生姓名+日期散列算出的假数 | **计数器**，预约真扣、取消真还 |
| 会话流程 | 查号源 → 直接写 CONFIRMED | 查重 → **预占** → 落库 HOLDING → **确认** → CONFIRMED |
| 失败回滚 | 无 | 确认失败 → **释放预占**，号源归还 |
| 退号 | 只改本地状态 | **HIS 侧真退号**，号源归还 |
| 不确定态 | 无 | 超时 → **UNKNOWN** → 对账 Job 收敛 |
| 号满场景 | 约 10% 概率随机满 | 号池真的会被占满（可构造） |
| 能验证的接口 | 只有"查" | 查 / 预占 / 确认 / 释放 / 退号 / 查单（全套） |

一句话：`mock` 只能证明"接口能调通"，`local_his` 能证明**业务流程正确**。

---

## 三、本地能验证的完整链路

.. code-block:: text

    查询是否有号源   → 剩余号 = limit − 已挂号 − 已预占（真实计数）
    预约挂号         → 审批弹窗 → 预占（号被占住）→ 落单（号真被扣）
                     → 本地 CONFIRMED + 外部挂号单号 + patient_ref
    取消预约挂号     → **先退 HIS（号归还）** → 本地 CANCELLED
    查询科室专家     → 医生名单与号源一致（号源从同一份排班生成）

外加健壮性场景：

* 重复预约 → 本地查重前置，**不重复占号**；
* 号源耗尽 → 明确失败（不是"永远有号"的假象）；
* HIS 超时 → 用户看到「结果正在核实」，本地 UNKNOWN，对账 Job 收敛；
* 号源状态持久化 → 重启后不丢。

---

## 四、一键验收脚本

后端起来之后：

```bash
cd guiguXiaozhi_backend_py
./.venv/bin/python scripts/verify_local_booking.py --base-url http://127.0.0.1:8081
```

脚本会自己登录、建会话、走审批、查库核对，最后打印结论表：

```
=== 本地预约挂号全流程验收（http://127.0.0.1:8081）===

--- 1. 查询是否有号源 ---
✅ 查询是否有号源：返回真实剩余号数 —— 共5个号

--- 2. 预约挂号 ---
✅ 预约挂号：审批后真实落单 —— 预约成功。预约详情：……
✅ 数据库：预约记录 CONFIRMED —— status=CONFIRMED
✅ 数据库：外部挂号单号已落库（对账依据）—— external_booking_id=reg-3
✅ 数据库：患者引用已落库（P0-06 方向）—— patient_ref=0ee1717df1b1d9ec…

--- 3. 号源扣减核对 ---
✅ 本地 HIS：号源已挂号数 current_num > 0（号真的被扣了）—— 命中 1 个号表实例

--- 4. 重复预约 ---
✅ 重复预约：被识别为重复 —— 您在呼吸内科…已有预约，请勿重复预约。
✅ 重复预约：号源未被重复占用 —— current_num 1 → 1

--- 5. 取消预约挂号 ---
✅ 取消预约：HIS 侧退号成功 —— 取消预约成功。
✅ 数据库：预约记录 CANCELLED（状态迁移，不物理删除）—— status=CANCELLED
✅ 本地 HIS：退号后号源已归还（current_num 回落）—— current_num=0
✅ 本地 HIS：无残留预占（hold 全部释放）—— holds=0

--- 6. 查询科室专家 ---
✅ 查询科室专家：返回医生名单 —— 徐国兴（呼吸内科 主任医师）…

=== 结论：11/11 项通过 ===
本地预约挂号全流程可用（查号源 / 预约 / 取消 / 查专家），号源扣减与归还均真实。
```

脚本支持 `--date` / `--slot` / `--department` 换测试数据。

**脚本会自动挑一个"该科室还没约过"的工作日**（`_pick_free_working_day`），
所以可以反复跑，每次测的都是**新建预约**而不是幂等命中。

> ⚠️ 验收脚本用的患者姓名必须是**纯中文**（带数字会被模型/校验器拒绝：
> "系统提示患者姓名不应包含数字"）；身份证号必须过 **GB 11643 校验位**
> （结构 = 区域码 6 + 生日 8 + 顺序码 **3** + 校验位 1）。
> 查库按**日期 + 科室**定位（不按姓名/身份证）—— 模型会"规范化"用户输入
> （实测把 `...9809` 落库成 `...0003`），按姓名查会误判为"没落库"。

---

## 五、号源从哪来

`local_his` 的号源**从本院真实医生排班生成**（`xiaozhi/knowledge/doctors.md`），
所以：

* 「查询科室专家」返回的医生 = 号源池里的医生（不会出现"约不到但能查到"）；
* 每个医生每天的限号数由 `LOCAL_HIS_LIMIT_NUM` 控制（默认 5）；
* 出诊日 = 周一至周五（与 Mock 一致），上午/下午按医生序号错开。

想让某个科室的号更紧张，改 `LOCAL_HIS_LIMIT_NUM=1` 就能验证"号满"分支。

---

## 六、独立运行（调试用）

本地 HIS 也可以单独跑（便于用 curl 观察协议）：

```bash
cd guiguXiaozhi_backend_py
./.venv/bin/python -m xiaozhi.service.local_his 9099
```

协议与真实 HIS 一致：

```bash
curl 'http://127.0.0.1:9099/clinicForRegist/list?clinicDate=2026-12-09&deptName=呼吸内科'
# {"code":200,"msg":"ok","data":[{"clinicForRegistId":"...","limitNum":5,"currentNum":0,...}]}

curl -X POST 'http://127.0.0.1:9099/clinicForRegist/hold' \
  -H 'Content-Type: application/json' \
  -d '{"clinicDate":"2026-12-09","timeDesc":"上午","deptName":"呼吸内科"}'
# {"code":200,"msg":"ok","data":{"holdId":"hold-1","clinicMasterId":"reg-1"}}
```

> ⚠️ 独立运行用的是**示例排班**（呼吸内科徐国兴 + 心内科张抒扬）；
> 要与应用的排班一致，请用应用内嵌模式（`SLOT_SERVICE_IMPL=local_his`）。

---

## 七、生产为什么禁用

`config_guard` 第 8 项：production 下 `slot_service_impl=local_his` **直接拒绝启动**。

理由：本地 HIS 替身的数据只在本机（重启即丢、换个副本就不一致），
**不具备医院侧事实效力**。用它上生产，比 Mock 更危险 —— Mock 至少不会让人
误以为真挂号了，而本地 HIS 会给出"看起来完全正常"的假成功。

生产的唯一合法路径：`slot_service_impl=his` + 真实沙箱验收通过 +
把 `"his"` 登记进 `config_guard.IMPLEMENTED_REAL_SLOT_ADAPTERS`。

---

## 八、已知限制

1. **号源按需展开**：真实 HIS 提前把排班展开成未来 N 天的号表实例，
   本地是查询时按需生成（语义等价：同一日期永远同一条记录，计数器不丢）。
2. **没有患者主档**：真实 HIS 挂号单要挂 `PatMasterIndex` 主索引，
   本地只存 `patient_ref`（我们生成的脱敏引用），不建 HIS 侧患者档案。
3. **退号端点路径未对齐真实 HIS**：本地用 `/currentRegister/cancel`，
   真实 HIS 的退号接口**未调研**（见 `docs/his-sandbox-onboarding.md`），
   沙箱到位后改 `HIS_CANCEL_PATH` 即可。
4. **HIS 侧幂等语义未验证**：本地 HIS 支持幂等（`X-Idempotency-Key`），
   真实 HIS 是否支持**未知** —— 这是上线前必须确认的项。
