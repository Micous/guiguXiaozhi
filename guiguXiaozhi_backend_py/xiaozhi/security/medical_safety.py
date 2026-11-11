"""医疗安全门控 —— Phase 7 / 产品医疗边界方案 A（保守）。

**产品边界（已拍板，方案 A）**：

    只做「信息解释 + 分诊引导」。急症确定性分流到固定急救话术；
    明确不回答诊断、处方、用药剂量。

为什么这层必须是**确定性规则**，而不是"在提示词里求模型注意"：

1. **急救指引不能等模型**。qwen2.5:7b 面对"我爸胸痛晕倒了"会输出
   "建议尽快到医院就诊"——语气温和、方向全错：真正的处置是立刻拨 120，
   并且**不要自行搬动/喂水**。这类回答的错误代价是生命，不能交给概率。
2. **模型会绕过提示词**。历史上"叙述承诺正则"被换了七八种说法绕过
   （见 踩坑与优化记录 第十节），诊断/处方同理：只要模型还在自由生成，
   就一定会有"你这个像是××病，可以先吃××"漏出来。所以越界请求
   **不进入生成**，直接给固定话术。
3. **审计要可举证**。"我们做了急症分流"必须对应到"这条规则、这次命中、
   这条话术"，而不是"我们提示过模型"。

判定顺序（顺序即优先级，改动前先看 tests/data/medical_safety_eval.json）：

    自伤风险 → 急症 → 越界（诊断/处方/剂量）→ 放行（+边界提示）

急症判定为什么不只看关键词：

    "胸痛挂什么科"是**咨询科室**（该答心内科门诊）；
    "我胸痛，现在还冒冷汗"是**急症**（该拨 120）。
    纯关键词会把前者也甩急救话术，用户会觉得系统"听不懂人话"。
    所以强信号（晕倒/大出血/意识丧失…）单独触发；弱信号（胸痛/呼吸困难…）
    需要额外的**主体**（我/我爸/孩子…）或**当下性**（现在/突然/一直…）才触发。

一个刻意的取舍：**"我胸痛挂什么科"仍走急救分流**。它带了主体，规则判为急症。
这是保守立场的正常代价——多分流一次只是"多一句废话"，漏分流一次可能要命。
评测集里这条被显式标注为 emergency，改动它会红。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Pattern, Tuple

# ---------------------------------------------------------------------------
# 固定话术（**逐字确定**：不含药物名、剂量、诊断结论）
#
# 这几段文本是合规交付物，不是文案。改动必须同步
# tests/unit/test_phase7_medical_safety.py 里的文案断言。
# ---------------------------------------------------------------------------

EMERGENCY_REPLY = (
    "这可能是需要立即处理的紧急情况，请不要等待在线回复。\n\n"
    "1. 立刻拨打 120 急救电话，或让身边的人代为拨打；必要时同时拨打 110。\n"
    "2. 不要自行驾车送医，也不要让患者独自等待——救护车在途中就可以开始处置。\n"
    "3. 等待期间：让患者保持安静；意识不清或呕吐时让其侧卧，防止误吸；\n"
    "   不要喂水、喂药，不要摇晃患者。\n"
    "4. 如患者没有呼吸、叫不醒，请按 120 调度员的电话指导进行心肺复苏；\n"
    "   现场有 AED（自动体外除颤器）时，按它的语音提示使用。\n\n"
    "本系统不能判断病情轻重，也无法调度急救资源。请以 120 急救人员的指导为准。"
)

SELF_HARM_REPLY = (
    "你提到的内容让我很担心你的安全，请现在就寻求即时帮助，不要独自承受。\n\n"
    "1. 如果有立即伤害自己的打算，请立刻拨打 120 或 110，或前往最近医院的急诊；\n"
    "2. 也可以拨打当地心理援助热线（可请 120 或医院总机转接），与专业人员通话；\n"
    "3. 请告诉身边一位你信任的人你现在的状态，尽量先不要独处。\n\n"
    "本系统是医院服务助手，不能提供心理危机干预。你的感受值得被认真对待，"
    "请让专业人员现在就来帮你。"
)

OUT_OF_SCOPE_REPLY = (
    "这个问题我不能回答：我不能做诊断、不能推荐药品或用药剂量，"
    "也不能判断检查结果是否正常。\n\n"
    "原因很实际——这些判断需要医生结合你的病史、体征和检查当面评估，"
    "只凭文字给结论是有风险的。\n\n"
    "我可以帮你做这些：\n"
    "1. 说明某个科室看什么病、怎么预约、就诊需要带什么材料；\n"
    "2. 解释公开的、通用的健康常识（不含诊断结论与用药建议）；\n"
    "3. 帮你预约对应科室的门诊，由医生当面判断。\n\n"
    "需要我帮你查号源或预约门诊吗？"
)

# 放行但注入模型 system 提示（医疗相关、非急症、非越界的正常咨询）
#
# 第 2 条为什么连"非处方药/感冒药/退烧药"这类**类别**建议也禁：
# 真机验收时（"感冒了怎么办"）7B 遵守了"不点名具体药品"，但输出了
# "可以使用一些非处方药物缓解症状，如感冒药、退热药等"——字面没违规，
# 实质仍是用药建议。保守立场要求把这条口子也堵上：只引导就医/咨询药师。
BOUNDARY_PROMPT = (
    "【医疗内容边界（必须遵守）】\n"
    "1. 只解释公开的通用健康知识与就诊流程；不得给出诊断结论"
    "（如“您这是××病”）、不得判断检查结果是否正常、不得替代医生面诊。\n"
    "2. 不得建议用户自行使用任何药物 —— 既包括点名具体药品与剂量，"
    "也包括“吃点感冒药/退烧药/非处方药”这类类别建议；"
    "只能建议就医、或咨询药师后按说明书使用。\n"
    "3. 涉及个人具体病情时，说明“需要医生当面评估”，并引导线下就诊或预约门诊。\n"
    "4. 不得声称“已确诊”“已排除××”“不用去医院”。\n"
    "5. 若用户描述的是正在发生的急症（胸痛、呼吸困难、大出血、意识障碍、抽搐等），"
    "直接提示立即拨打 120，不要先做解释。"
)

# 判定结果类型
OK = "ok"
EMERGENCY = "emergency"
SELF_HARM = "self_harm"
OUT_OF_SCOPE = "out_of_scope"


@dataclass(frozen=True)
class SafetyVerdict:
    """一次医疗安全判定。``blocked`` 为真时**不进检索、不进模型**。"""

    kind: str = OK
    rule_id: str = ""
    reply: str = ""
    # 是否附加边界提示（kind=ok 时也可能为真：正常医疗咨询需要约束模型口径）
    boundary_prompt: bool = False

    @property
    def blocked(self) -> bool:
        return self.kind in (EMERGENCY, SELF_HARM, OUT_OF_SCOPE)

    def to_metadata(self) -> dict:
        """进 trace / 事件用的最小信息（不含用户原文，避免敏感内容扩散）。"""
        return {
            "kind": self.kind,
            "rule_id": self.rule_id,
            "blocked": self.blocked,
            "boundary_prompt": self.boundary_prompt,
        }


# ---------------------------------------------------------------------------
# 规则表
# ---------------------------------------------------------------------------

# 1) 自伤/自杀风险：优先于一切（"胸痛+不想活"要先接住人）。
_SELF_HARM_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("self_harm.suicide", r"自杀|轻生|想死|不想活|活着没意思|结束生命|了结自己"),
    (
        "self_harm.method",
        r"割腕|跳楼|上吊|服毒|安眠药.{0,10}(?:自杀|了结|能死|会死|致死)"
        r"|(?:吃|喝|吞|服)(?:多少|几片|几粒).{0,10}(?:能|会)(?:死|致命)",
    ),
)

# 2) 急症强信号：单独命中即分流（这些表述没有"科普式提问"的可能）。
_EMERGENCY_STRONG: Tuple[Tuple[str, str], ...] = (
    ("emergency.unresponsive", r"叫不醒|喊不醒|叫不应|意识不清|意识丧失|昏迷|不省人事|晕倒|昏倒|昏厥"),
    ("emergency.seizure", r"抽搐|抽风|惊厥|口吐白沫"),
    ("emergency.shock", r"休克|面色惨白|四肢冰冷.{0,6}(?:出冷汗|大汗)"),
    ("emergency.airway", r"没有呼吸|停止呼吸|呼吸骤停|窒息|噎住|卡在喉咙|气道梗阻|掐住脖子"),
    # "喘不上气/呼吸不上来"是**正在发生**的表述（没人在健康时这么说），
    # 单独即可分流；科普式提问（"呼吸困难的原因有哪些"）由下面的
    # _EDU_RE 豁免兜住。
    ("emergency.dyspnea_severe", r"呼吸困难|喘不上气|喘不过气|呼吸不上来|上不来气|吸不上气"),
    ("emergency.bleeding", r"大出血|大量出血|血止不住|止不住血|喷血|呕血|吐血|咯血|咳血"),
    ("emergency.stroke", r"口角歪斜|嘴角歪|嘴巴歪|半身不遂|偏瘫|半边(?:身子|身体|身)|一侧肢体(?:无力|不能动)|说话不清|口齿不清|言语不清|说不了话|讲不出话|吐字不清"),
    ("emergency.cardiac", r"剧烈胸痛|胸口剧痛|压榨性疼痛|濒死感|胸痛.{0,6}(?:大汗|冷汗|晕)"),
    ("emergency.anaphylaxis", r"喉头水肿|严重过敏|全身(?:起)?皮疹.{0,8}呼吸困难"),
    ("emergency.poisoning", r"误服|吃错药|喝(?:了)?农药|药物中毒|一氧化碳中毒|煤气中毒"),
    ("emergency.obstetric", r"孕.{0,4}(?:大出血|腹痛.{0,4}出血)|临产|脐带脱垂"),
)

# 3) 急症弱信号：需搭配「主体」或「当下性」才分流（否则可能只是咨询科室）。
_EMERGENCY_WEAK: Tuple[Tuple[str, str], ...] = (
    ("emergency.chest", r"胸痛|胸闷|胸口(?:发闷|闷|疼|压迫)|心前区(?:疼|痛)"),
    ("emergency.dyspnea", r"呼吸困难|喘不上气|喘不过气|呼吸不上来|上不来气|吸不上气|气短|呼吸急促|憋气"),
    ("emergency.palpitation", r"心慌|心悸|心跳(?:很快|过快|乱了)"),
    ("emergency.abdomen", r"肚子(?:剧痛|疼得厉害)|腹部剧痛|剧烈腹痛"),
    ("emergency.fever", r"高烧不退|高热不退|烧到 ?(?:39|40)|体温 ?(?:39|40)"),
    ("emergency.trauma", r"摔(?:了|伤)|车祸|撞伤|头部外伤|骨折|烫伤|烧伤"),
    ("emergency.bleeding_mild", r"出血不止|流血不止|一直流血"),
)

_SUBJECT_RE = re.compile(
    r"我(?:爸|妈|母亲|父亲|家|们家)?|孩子|宝宝|小(?:孩|儿)|儿子|女儿|婴儿|新生儿"
    r"|老人|爷爷|奶奶|外公|外婆|姥姥|姥爷|家人|老公|老婆|丈夫|妻子|病人|患者|同事|朋友"
)
_PRESENT_RE = re.compile(
    r"现在|正在|突然|刚刚|刚才|一直|还在|持续|已经|今天|这会|这会儿|目前|越来越|加重"
)
# 求助口吻：没有主体、也没有时间词，但明确在问"要不要就医"——同样是当下症状。
# 不把它算作上下文的话，"胸痛，需要去医院吗"会被当成科室咨询放行，
# 而这恰恰是最需要分流的一类问法。
_SEEKING_HELP_RE = re.compile(
    r"怎么办|要不要去医院|需要去医院|该不该去|是不是很严重|严不严重|要不要紧|看急诊|去急诊"
)

# 科普式提问的豁免标记：**必须同时**没有主体/当下性/求助口吻才豁免。
#
# 为什么需要这层：强信号里"晕倒""呼吸困难"本身就可能是科普题面
# （"晕倒的原因有哪些"）。只按关键词分流会把知识问答甩成急救话术，
# 而真正的求助一定会带主体或时间（"我爸晕倒了""突然晕倒"），
# 豁免逻辑因此不会漏掉真急症。
_EDU_RE = re.compile(
    r"原因|病因|诱因|有哪些(?:表现|症状|原因)|是什么病|为什么会|科普|机制|原理|区别|预防"
)
# 已有患者身份信息（正在填单办理业务）：弱信号不再触发急救分流，
# 否则"我爸胸痛，帮我预约心内科，身份证…"会被急救话术打断挂号流程。
_ID_CARD_RE = re.compile(r"\d{15,18}[\dXx]?")

# 4) 越界：诊断结论 / 处方 / 用药剂量 / 报告下结论。
#    这些一律不进入模型生成 —— 只要模型还能自由发挥，就一定会漏出
#    "你这个像是××，先吃点××"（提示词拦不住，历史上已被验证过多次）。
_OUT_OF_SCOPE: Tuple[Tuple[str, str], ...] = (
    (
        # 动词 + "药"的泛指（"吃感冒药可以吗""想开点药"）都算越界：
        # 具体药名与搭配禁忌属通用常识，但"哪种药适合我"必须当面判断。
        "oos.prescribe",
        r"(?:吃|用|服|买|开|配|挂)(?:点|些|什么|啥|个)?[\u4e00-\u9fa5]{0,5}药"
        r"|开(?:个|一张)?处方|推荐.{0,6}(?:药|偏方|中成药)|用什么药",
    ),
    (
        "oos.dosage",
        r"剂量|吃几片|吃几粒|几片|几粒|多少毫克|\d+\s*(?:mg|毫克|g|片|粒)"
        r"|一天(?:吃|服)?几次|一日(?:吃|服)?几次|一次(?:吃|服)多少",
    ),
    (
        "oos.diagnosis",
        r"我(?:是不是|有没有)(?:得|患|有)|是不是(?:得|患)了|帮我(?:诊断|确诊)"
        r"|我这是(?:什么|啥)病|我得了(?:什么|啥)病|确诊一下|诊断一下|判断一下.{0,6}病"
        r"|(?:这个|我的)(?:病|症状).{0,6}(?:严重吗|要紧吗|是什么病)",
    ),
    (
        "oos.report",
        r"(?:化验|检验|检查|体检|影像|CT|核磁|B超)(?:单|报告|结果).{0,12}"
        r"(?:正常吗|严重吗|要紧吗|有问题吗|没问题吧|什么意思|怎么看|解读)",
    ),
    # 直接索取肿瘤/重症结论，属于高危越界（线上给结论风险最大）
    ("oos.critical", r"(?:是不是|有没有)(?:癌症|肿瘤|恶性|白血病)|(?:还能活|能活多久|活多久)"),
)

# 5) 医疗相关（放行但要约束口径）
_MEDICAL_TERMS_RE = re.compile(
    r"病|症|痛|疼|药|医嘱|手术|检查|化验|指标|血压|血糖|血脂|发烧|发热|咳嗽"
    r"|感冒|炎|瘤|癌|过敏|怀孕|产检|疫苗|副作用|忌口|饮食|康复|护理|麻醉"
    # 常见主诉/状态词：这些不带"病"字，但同样会引出用药或诊断类回答
    r"|失眠|抑郁|焦虑|头晕|头痛|乏力|恶心|呕吐|腹泻|便秘|皮疹|瘙痒|水肿"
    r"|化疗|放疗|透析|输液|复查|门诊|急诊"
)

_SELF_HARM_COMPILED: List[Tuple[str, Pattern[str]]] = [
    (rid, re.compile(pat)) for rid, pat in _SELF_HARM_PATTERNS
]
_STRONG_COMPILED: List[Tuple[str, Pattern[str]]] = [
    (rid, re.compile(pat)) for rid, pat in _EMERGENCY_STRONG
]
_WEAK_COMPILED: List[Tuple[str, Pattern[str]]] = [
    (rid, re.compile(pat)) for rid, pat in _EMERGENCY_WEAK
]
_OOS_COMPILED: List[Tuple[str, Pattern[str]]] = [
    (rid, re.compile(pat)) for rid, pat in _OUT_OF_SCOPE
]


def _first_match(text: str, rules: List[Tuple[str, Pattern[str]]]) -> Optional[str]:
    for rule_id, pattern in rules:
        if pattern.search(text):
            return rule_id
    return None


def _is_educational(text: str) -> bool:
    """是否是科普式提问（有科普标记，且完全没有主体/当下性/求助口吻）。"""
    if not _EDU_RE.search(text):
        return False
    return not (
        _SUBJECT_RE.search(text)
        or _PRESENT_RE.search(text)
        or _SEEKING_HELP_RE.search(text)
    )


def assess_medical_safety(user_input: str) -> SafetyVerdict:
    """确定性判定一条用户消息的医疗安全处置（纯函数，无 IO、无模型）。"""
    text = (user_input or "").strip()
    if not text:
        return SafetyVerdict()

    # ---- 1. 自伤风险（最高优先）----
    rule = _first_match(text, _SELF_HARM_COMPILED)
    if rule:
        return SafetyVerdict(kind=SELF_HARM, rule_id=rule, reply=SELF_HARM_REPLY)

    # ---- 2. 急症强信号（科普式提问豁免）----
    rule = _first_match(text, _STRONG_COMPILED)
    if rule and not _is_educational(text):
        return SafetyVerdict(kind=EMERGENCY, rule_id=rule, reply=EMERGENCY_REPLY)

    # ---- 3. 急症弱信号 + 主体/当下性 ----
    weak_rule = _first_match(text, _WEAK_COMPILED)
    if weak_rule:
        has_context = bool(
            _SUBJECT_RE.search(text)
            or _PRESENT_RE.search(text)
            or _SEEKING_HELP_RE.search(text)
        )
        filling_form = bool(_ID_CARD_RE.search(text))
        if has_context and not filling_form:
            return SafetyVerdict(kind=EMERGENCY, rule_id=weak_rule, reply=EMERGENCY_REPLY)

    # ---- 4. 越界（诊断/处方/剂量/报告结论）----
    # 注意顺序：急症先于越界。"我爸胸痛该吃什么药"要的是 120，不是边界声明。
    rule = _first_match(text, _OOS_COMPILED)
    if rule:
        return SafetyVerdict(kind=OUT_OF_SCOPE, rule_id=rule, reply=OUT_OF_SCOPE_REPLY)

    # ---- 5. 放行；医疗相关则附加口径约束 ----
    return SafetyVerdict(
        kind=OK,
        boundary_prompt=bool(_MEDICAL_TERMS_RE.search(text)),
    )


def boundary_prompt_for(verdict: SafetyVerdict) -> str:
    """需要注入 system 的边界提示（不需要时返回空串）。"""
    return BOUNDARY_PROMPT if verdict.boundary_prompt else ""
