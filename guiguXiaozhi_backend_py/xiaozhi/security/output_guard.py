"""医疗回复出口扫描 —— Phase 9-A（Phase 7 的纵深防御第二层）。

入口拦截（:mod:`xiaozhi.security.medical_safety`）管的是"用户问的是不是越界请求"，
本模块管的是**模型自己漏出来的内容**。两层为什么要都在：

    "感冒了怎么办"      是正常咨询（入口放行），但 7B 会答
                        "可以使用一些非处方药物（感冒药、退热药）" ← 只有出口能拦；
    "布洛芬一天吃几次"  入口直接拦（进不了模型），出口用不上。

Phase 7 验收时真机撞到过第一类（提示词收紧后才收住）。但**提示词是概率约束**：
换一句问法、换一个模型版本、换一次温度都可能再漏一次。所以出口再加一层
**确定性扫描**——命中即就地丢弃该句，并在结尾追加固定说明。

设计上的三条硬约束：

1. **只能按句处理，不能整段扣住**。医疗咨询的回答动辄 300+ 字、7B 要跑 30~70 秒，
   整段缓冲会让首字延迟变成"整整一分钟白屏"。所以按句边界（。！？换行）逐句放行：
   用户感觉仍是流式，只是每句晚一个标点。
2. **我们自己的话术必须放行**。`OUT_OF_SCOPE_REPLY` 里有"不能推荐药品"、
   `EMERGENCY_REPLY` 里有"不要喂水、喂药"—— 带否定词的表述一律不算命中，
   否则出口扫描会把合规话术自己也删掉（真发生过：`不要自行服用` 命中"服用"）。
3. **命中要说得出规则号**。`rule_id` 会进指标与日志，用于回答
   "为什么这条回复被改写了"，而不是靠猜。

已知边界（写在这里免得被当成"已解决"）：

* 扫描的是**中文药品通用名与常见商品名**，不是完整药典；新药、院内制剂、
  英文名（ibuprofen）不在词典里。它的定位是"降低漏出概率"，不是"保证不漏"；
* 剂量规则只认"数字+单位"与"频次"两种形态，纯口语化的"多吃两片"只靠药名兜住；
* 被丢弃的句子不会补回来（**改写不做生成**）——宁可少说一句，也不让模型
  再生成一遍去补。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Pattern, Tuple

# ---------------------------------------------------------------------------
# 固定文案（合规交付物：不得含药名、剂量、诊断结论；改动要同步单测断言）
# ---------------------------------------------------------------------------

#: 部分句子被丢弃时，结尾追加的说明。
OUTPUT_GUARD_SUFFIX = (
    "\n\n（说明：涉及具体用药与诊断判断的内容我不能给出建议。"
    "如需用药请咨询药师或到院就诊，我可以帮您预约相应科室。）"
)

#: 整段回复都命中时，用这段替换（不留模型的任何原句）。
OUTPUT_GUARD_REPLY = (
    "关于具体用药、剂量和诊断判断，我不能给出建议，也不替您判断病情。\n\n"
    "原因很实际——这些判断需要医生结合病史、体征和检查当面评估，"
    "只凭文字给结论是有风险的。\n\n"
    "我可以帮你做这些：\n"
    "1. 说明某个科室看什么病、怎么预约、需要带什么材料；\n"
    "2. 解释公开的、通用的健康常识（不含用药建议与诊断结论）；\n"
    "3. 帮你预约对应科室的门诊，由医生当面判断。\n\n"
    "需要我帮你查号源或预约门诊吗？"
)

#: 命中类型
MEDICATION = "medication"
DIAGNOSIS = "diagnosis"

# ---------------------------------------------------------------------------
# 规则表
# ---------------------------------------------------------------------------

#: 具体药品名（通用名 + 常见商品名）。命中即判越界 —— 方案 A 不含任何用药建议，
#: 连"这个药是干什么的"也不由本系统回答（用户该问药师）。
_DRUG_TERMS: Tuple[str, ...] = (
    # 解热镇痛
    "布洛芬", "对乙酰氨基酚", "扑热息痛", "阿司匹林", "双氯芬酸", "洛索洛芬",
    "芬必得", "美林", "泰诺", "百服宁", "安乃近",
    # 抗感染
    "头孢", "阿莫西林", "青霉素", "阿奇霉素", "红霉素", "罗红霉素", "克拉霉素",
    "左氧氟沙星", "氧氟沙星", "莫西沙星", "甲硝唑", "氟康唑", "阿昔洛韦", "奥司他韦",
    # 消化
    "奥美拉唑", "雷贝拉唑", "泮托拉唑", "兰索拉唑", "铝碳酸镁", "蒙脱石散", "蒙脱石",
    "思密达", "多潘立酮", "吗丁啉", "莫沙必利", "乳果糖", "开塞露", "益生菌",
    # 呼吸/抗过敏
    "氯雷他定", "西替利嗪", "扑尔敏", "氯苯那敏", "孟鲁司特", "氨溴索", "溴己新",
    "右美沙芬", "愈创甘油醚", "沙丁胺醇", "布地奈德", "氨茶碱", "乙酰半胱氨酸",
    # 心血管/内分泌
    "硝酸甘油", "速效救心丸", "复方丹参滴丸", "硝苯地平", "氨氯地平", "美托洛尔",
    "比索洛尔", "厄贝沙坦", "缬沙坦", "卡托普利", "氢氯噻嗪", "呋塞米", "阿托伐他汀",
    "二甲双胍", "格列美脲", "阿卡波糖", "胰岛素", "左甲状腺素", "甲巯咪唑",
    # 激素/其他
    "地塞米松", "泼尼松", "甲泼尼龙", "氢化可的松", "云南白药", "红花油", "创可贴",
    # 中成药（含"清热/解毒"类常见品）
    "连花清瘟", "板蓝根", "双黄连", "藿香正气", "感冒灵", "维C银翘", "蒲地蓝",
    "蓝芩", "抗病毒口服液", "复方甘草片", "川贝枇杷",
)

#: 药品**类别**（不点名也属于用药建议 —— Phase 7 真机就是漏在这一层）。
_DRUG_CLASSES: Tuple[str, ...] = (
    "非处方药", "处方药", "退烧药", "退热药", "止痛药", "镇痛药", "消炎药",
    "抗生素", "抗菌药", "感冒药", "止咳药", "化痰药", "止泻药", "泻药", "通便药",
    "抗过敏药", "抗组胺药", "安眠药", "镇静剂", "激素类药", "外用药膏", "眼药水",
)

_DRUG_RE: Pattern[str] = re.compile("|".join(_DRUG_TERMS + _DRUG_CLASSES))

#: 剂量与频次（"一次一片""每日 3 次""0.3g"）。单位表刻意保守：
#: 只收药物剂量的常见单位，避免把"每天走 8000 步"这类误判。
_DOSAGE_RE: Pattern[str] = re.compile(
    r"\d+(?:\.\d+)?\s*(?:mg|毫克|μg|ug|g|克|ml|毫升|iu|单位|片|粒|袋|支|滴|喷|丸|贴)"
    r"|(?:一次|每次|每日|每天|一日|一天|每顿|每 ?\d+ ?小时)\s*[0-9一二三四五六两半]+\s*"
    r"(?:片|粒|袋|支|丸|滴|喷|次|ml|毫升)"
    r"|[0-9一二三四五六两]+\s*次\s*[／/]?\s*(?:日|天)"
    r"|(?:每日|每天|一日|一天)\s*[0-9一二三四五六两]+\s*次",
    re.IGNORECASE,
)

#: 处方口吻（"建议服用""可以先含服"）。**必须**排除否定句：
#: "不要自行服用任何药物" 是我们自己的合规话术，不能拦。
#:
#: 动词表刻意只收**制药专用**的词：`吃` / `用` 太泛，会把
#: "可以先用物理降温，比如温水擦浴" 判成用药建议（真机误伤过一次）。
#: 泛动词的宾语几乎总是药名或药品类别，那两条规则已经覆盖了。
_RX_VERB_RE: Pattern[str] = re.compile(
    r"(?:建议|可以|可先|可以先|最好|应该|不妨|需要)\s*"
    r"(?:服用|口服|冲服|含服|外涂|涂抹|外敷|滴眼|滴鼻|喷雾|雾化|嚼服|注射|静滴)"
)
_NEGATION_RE: Pattern[str] = re.compile(
    r"不要|不得|不可|不能|不应|不宜|禁止|请勿|避免|勿|别|切勿|无需|不用"
)

#: 诊断结论口吻。刻意不含裸 "不用担心"（正常安抚语），只收**明确的结论性**表述。
_DIAGNOSIS_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("diagnosis.this_is", r"(?:您|你)(?:这|的(?:情况|症状|病))(?:就)?是[^。！？\n]{0,12}(?:病|炎|症|感染|过敏)"),
    ("diagnosis.inferred", r"(?:大概率|很可能|多半|基本上?(?:就)?|应该(?:就)?)(?:是|属于)[^。！？\n]{0,10}(?:病|炎|症|感染|过敏|紊乱)"),
    ("diagnosis.confirmed", r"确诊为|已确诊|可以确诊|诊断为"),
    ("diagnosis.excluded", r"可以排除|基本排除|已排除|不(?:是|属于)[^。！？\n]{0,10}(?:病|炎|症)"),
    ("diagnosis.reassure", r"不用去医院|无需就医|不用看医生|不必就医|不需要就医|没什么大碍|问题不大|不用治疗"),
    ("diagnosis.report", r"(?:检查|化验|报告|结果|片子)[^。！？\n]{0,8}(?:正常|没问题|没事|良好|无异常)"),
)

#: 这些规则**带否定词就不算命中**。
#:
#: 典型误伤：我们自己的话术"我不能判断检查结果是否正常"里同时含
#: "检查结果"和"正常"，会被 ``diagnosis.report`` 判成诊断结论 → 把自己的合规
#: 话术删掉。而 ``diagnosis.reassure`` 恰恰**本身含** "不用/无需"，
#: 所以否定豁免只能按规则开，不能全局开。
_NEGATION_SENSITIVE_RULES = frozenset({"diagnosis.report"})


def _compile(patterns: Tuple[Tuple[str, str], ...]) -> Tuple[Tuple[str, Pattern[str]], ...]:
    return tuple((rule_id, re.compile(pattern)) for rule_id, pattern in patterns)


_DIAGNOSIS_RULES = _compile(_DIAGNOSIS_PATTERNS)


#: 承接词：上一句被丢弃后，紧跟着的转折句是"半截话"（"但记得要…"），
#: 语义挂在不存在的上半句上，读起来比不说更糟。这类句子跟着一起丢。
_ADVERSATIVE_PREFIX = ("但", "不过", "可是", "然而", "所以", "因此", "总之")


@dataclass(frozen=True)
class OutputVerdict:
    """一条回复片段的扫描结果。"""

    kind: str = ""          # ""（放行）/ medication / diagnosis
    rule_id: str = ""

    @property
    def hit(self) -> bool:
        return bool(self.kind)

    def to_metadata(self) -> dict:
        return {"kind": self.kind, "rule_id": self.rule_id}


def scan_medical_output(text: str) -> OutputVerdict:
    """扫一段文本（通常是一句），判断是否含用药/诊断内容。

    纯函数、无 IO、无模型。**空文本放行**。
    """
    if not text:
        return OutputVerdict()

    # 1) 诊断结论先判：它的规则最具体（含"可以排除××病"这类判词）。
    #    带否定词的表述必须放行（那是我们自己的合规话术），但仅限单条规则。
    negated = bool(_NEGATION_RE.search(text))
    for rule_id, pattern in _DIAGNOSIS_RULES:
        if negated and rule_id in _NEGATION_SENSITIVE_RULES:
            continue
        if pattern.search(text):
            return OutputVerdict(DIAGNOSIS, rule_id)

    # 2) 药品名 / 药品类别
    match = _DRUG_RE.search(text)
    if match:
        return OutputVerdict(MEDICATION, f"medication.drug:{match.group(0)}")

    # 3) 剂量与频次
    match = _DOSAGE_RE.search(text)
    if match:
        return OutputVerdict(MEDICATION, "medication.dosage")

    # 4) 处方口吻（带否定词的一律放行 —— 那是我们自己的合规话术）
    if not _NEGATION_RE.search(text) and _RX_VERB_RE.search(text):
        return OutputVerdict(MEDICATION, "medication.prescribe_verb")

    return OutputVerdict()


def screen_medical_output(text: str) -> str:
    """整段扫描版（用于非流式场景与测试）：命中即返回固定替换文案。"""
    return "" if scan_medical_output(text).hit else text


def strip_blocked_sentences(text: str) -> str:
    """按句丢弃命中片段（**不加说明**，用于要落库的叙述段）。

    为什么不复用 :class:`MedicalOutputGuard`：那个类会追加固定说明，
    而说明只该出现在"下发给用户的最终回复"里；叙述段是给模型看的，
    混进一句"我不能给用药建议"会变成下一轮模仿的句式。
    """
    if not text:
        return text
    kept: List[str] = []
    buffer = ""
    dropped_last = False
    for char in text:
        buffer += char
        if char in _SENTENCE_END:
            if dropped_last and buffer.lstrip().startswith(_ADVERSATIVE_PREFIX):
                pass  # 半截转折句：跟着上一句一起丢
            elif not scan_medical_output(buffer).hit:
                kept.append(buffer)
                dropped_last = False
                buffer = ""
                continue
            else:
                dropped_last = True
            buffer = ""
    if buffer and not scan_medical_output(buffer).hit:
        kept.append(buffer)
    return "".join(kept)


# ---------------------------------------------------------------------------
# 流式过滤器
# ---------------------------------------------------------------------------

#: 句子边界。与 :mod:`xiaozhi.runtime.stream_filter` 保持一致（含换行）。
_SENTENCE_END = "。！？!?\n；;"
#: 兜底切分：整句过长时按逗号切，避免"一句话没有标点"把首字无限押后。
_CLAUSE_END = "，,、：:"
#: 单句最多押后多少字（超过就按逗号/硬切放行）。医疗回答里 60 字已是很长的一句话。
MAX_HOLD = 60
#: 硬切时留给下一块的尾巴长度：防止药名/剂量被切成两半（"布洛|芬"）。
_CARRYOVER = 6


class MedicalOutputGuard:
    """按句扫描的流式输出护栏。

    用法与 :class:`~xiaozhi.runtime.stream_filter.StreamFilter` 一致::

        guard = MedicalOutputGuard()
        for delta in model_stream:
            safe = guard.feed(delta)     # 可下发的文本（可能是空串）
        safe = guard.flush()             # 收尾：剩余文本 + 固定说明

    关闭时 :meth:`feed` 原样返回、:meth:`flush` 返回空串（调用方逻辑不用改）。
    """

    def __init__(
        self,
        enabled: bool = True,
        metrics=None,
        trace_id: str = "",
        max_hold: int = MAX_HOLD,
    ) -> None:
        self.enabled = bool(enabled)
        self.metrics = metrics
        self.trace_id = trace_id
        self.max_hold = max(20, int(max_hold))
        self._buffer = ""
        self._blocked_rules: List[str] = []
        self._emitted = 0
        self._suffix_done = False
        # 上一句是否刚被丢弃（用于连带丢掉"但…"/"所以…"这类半截转折句）
        self._dropped_last = False

    # ---- 对外状态 ----
    @property
    def blocked_rules(self) -> List[str]:
        return list(self._blocked_rules)

    @property
    def blocked_any(self) -> bool:
        return bool(self._blocked_rules)

    def feed(self, text: str) -> str:
        if not self.enabled:
            return text
        if not text:
            return ""
        self._buffer += text
        out: List[str] = []
        while True:
            cut = self._find_boundary(self._buffer)
            if cut < 0:
                break
            sentence, self._buffer = self._buffer[:cut], self._buffer[cut:]
            out.append(self._screen(sentence))
        # 一整句迟迟没有标点（模型偶发）：按逗号/硬切放行，宁可粗一点也要保流式
        if len(self._buffer) >= self.max_hold:
            cut = self._soft_cut(self._buffer)
            if cut > 0:
                sentence, self._buffer = self._buffer[:cut], self._buffer[cut:]
                out.append(self._screen(sentence))
        return self._record("".join(out))

    def flush(self) -> str:
        if not self.enabled:
            return ""
        rest = self._screen(self._buffer) if self._buffer else ""
        self._buffer = ""
        if self.blocked_any and self._emitted + len(rest) == 0:
            # 整段都被拦掉：给固定话术（而不是只留一句"我不能给建议"）
            self._count("medical_output_blocked_total")
            return OUTPUT_GUARD_REPLY
        if self.blocked_any and not self._suffix_done:
            self._suffix_done = True
            self._count("medical_output_rewritten_total")
            rest += OUTPUT_GUARD_SUFFIX
        return self._record(rest)

    # ---- 内部 ----
    def _screen(self, sentence: str) -> str:
        if not sentence:
            return ""
        # 上一句刚被丢弃 → 紧跟的转折/结论句失去指代对象，一起丢
        if self._dropped_last and sentence.lstrip().startswith(_ADVERSATIVE_PREFIX):
            self._dropped_last = False
            self._blocked_rules.append("guard.dangling_continuation")
            self._count("medical_output_hits_total")
            return ""
        self._dropped_last = False
        verdict = scan_medical_output(sentence)
        if not verdict.hit:
            return sentence
        self._blocked_rules.append(verdict.rule_id)
        self._count("medical_output_hits_total")
        self._dropped_last = True
        return ""

    def _record(self, text: str) -> str:
        self._emitted += len(text)
        return text

    def _count(self, name: str) -> None:
        if self.metrics is not None:
            self.metrics.inc(name)

    @staticmethod
    def _find_boundary(text: str) -> int:
        for index, char in enumerate(text):
            if char in _SENTENCE_END:
                return index + 1
        return -1

    @staticmethod
    def _soft_cut(text: str) -> int:
        """超长无标点时的切点：优先最后一个逗号，其次硬切（留尾巴防切断药名）。"""
        for index in range(len(text) - 1, -1, -1):
            if text[index] in _CLAUSE_END:
                return index + 1
        cut = len(text) - _CARRYOVER
        return cut if cut > 0 else len(text)
