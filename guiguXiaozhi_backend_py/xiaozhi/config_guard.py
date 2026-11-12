"""生产启动门禁 —— APP_ENV=production 时 fail-fast 校验。

职责边界：
  * config.py       只负责「读配置」；
  * application.py  只负责「装配组件」；
  * 本模块          只负责「哪些配置组合允许对外服务」这一策略判断。

为什么需要它：原型默认值为了方便本地联调而刻意宽松——
``permission_no_ui=allow``、``mysql_password=123456``、号源用 Mock、
画像缺密钥时静默禁用。这些值在本地是「方便」，一旦有人把 APP_ENV 标成
production 就变成**静默高危放行**：无确认的写操作、弱口令、模拟号源、
降级路径全部照常启动，而 /health 依然返回 ok。

所以 production 必须在连接任何外部服务之前聚合报出全部问题并拒绝启动。
development 下本模块完全不生效，既有行为零变更。
"""

from __future__ import annotations

from typing import FrozenSet, List

from xiaozhi.config import Settings

# 弱口令/默认口令黑名单（小写比较）。生产禁止沿用 config.py 的默认值。
WEAK_MYSQL_PASSWORDS: FrozenSet[str] = frozenset(
    {
        "",
        "123456",
        "12345678",
        "123456789",
        "root",
        "password",
        "admin",
        "mysql",
        "test",
    }
)

# 已落地的**真实**号源适配器（不含 mock）。
# Phase 3b 实现 HisSlotService 后，需在 application._build_slot_service 注册，
# 并把适配器名加进这里，production 门禁才会放行。
# 目前为空 = 无论 slot_service_impl 填什么，production 都不得启动。
# 这是刻意的：没有真实 HIS，就不该存在「已可上线」的生产模式。
IMPLEMENTED_REAL_SLOT_ADAPTERS: FrozenSet[str] = frozenset()


class ProductionConfigError(RuntimeError):
    """生产配置不合规：拒绝启动（区别于运行期异常，便于运维一眼定位）。"""


def validate_production_settings(settings: Settings) -> None:
    """非生产直接返回；生产环境聚合全部问题后一次性抛出。

    一次报全而不是遇到第一个就退出：运维照着清单一次修完，避免
    「改一项、重启、再报下一项」的循环。
    """
    if not settings.is_production:
        return

    problems: List[str] = []

    # 1) 无审批 UI 时放行 ASK 级写操作 == 无确认挂号（审计 P0-02 / 评估 P0-3）
    if settings.permission_no_ui != "deny":
        problems.append(
            f"permission_no_ui={settings.permission_no_ui!r} → 必须为 'deny'："
            "无审批 UI 时放行 ASK 级写操作等于无确认挂号/取消"
        )

    # 2) 默认/弱数据库口令（审计 P1-05）
    if settings.mysql_password.strip().lower() in WEAK_MYSQL_PASSWORDS:
        problems.append(
            "mysql_password 仍是默认/弱口令 → 必须换成强密码并由环境注入，"
            "禁止使用 config.py 的默认值"
        )

    # 3) 画像开启但缺加密密钥：开发环境会「禁用 + 告警」静默降级，生产不允许
    if settings.profile_enabled and not settings.profile_secret_key.strip():
        problems.append(
            "profile_enabled=true 但 profile_secret_key 为空 → "
            "画像中的身份证号必须 Fernet 密文落库，生产禁止静默禁用降级"
        )

    # 4) Mock 号源 / HIS 适配器未实现（审计 P0-03 / 评估 P0-2）
    adapter = settings.slot_service_impl.strip().lower()
    if adapter not in IMPLEMENTED_REAL_SLOT_ADAPTERS:
        problems.append(
            f"slot_service_impl={settings.slot_service_impl!r} → "
            "当前仓库只有 MockSlotService，真实 HIS 适配器（Phase 3b）尚未实现；"
            "模拟号源不具业务效力，禁止以 production 启动"
        )

    # 5) 认证门禁（审计 P0-01）：生产必须开认证，且密钥材料齐备
    if not settings.auth_enabled:
        problems.append(
            "auth_enabled=false → 生产必须开启认证（JWT/OIDC）："
            "当前 memoryId/userId 由请求体自报，可越权读写他人会话"
        )
    else:
        if len(settings.auth_secret_key.strip()) < 32:
            problems.append(
                "auth_secret_key 为空或短于 32 字符 → HS256 密钥必须由环境注入且足够长"
            )
        if not settings.auth_bootstrap_secret.strip():
            problems.append(
                "auth_bootstrap_secret 为空 → 本地签发端点的换取凭证必须配置"
            )

    # 6) 审批闭环（审计 P0-02）：permission_no_ui=deny 只做到「失败关闭」，
    #    还需真正开启带持久化状态机的确认通道，否则写工具在生产永远不可用
    #    （或被人悄悄改回 allow 变成无确认挂号）。
    if not settings.approval_enabled:
        problems.append(
            "approval_enabled=false → 生产必须开启审批闭环（PENDING→APPROVED→CONSUMED）："
            "缺少用户明确确认与一次性 token 绑定，不得执行预约/取消"
        )

    # 7) 会话存储失败策略（审计 P1-01）：生产禁止把 Mongo 故障静默降级成
    #    本地文件 —— 本地文件不是多副本共享会话的合法故障转移目标，
    #    降级期间写的会话在恢复后成为"第二个数据源"，需要人工合并。
    if not settings.storage_fail_fast:
        problems.append(
            "storage_fail_fast=false → 生产必须开启会话存储快速失败（strict Mongo）："
            "禁止自动降级本地文件；Mongo 故障应表现为聊天 503 与 /ready 失败"
        )

    # 8) 号源适配器：本地 HIS 替身绝不能上生产。
    #    它是**开发/联调**用的行为替身（数据在本地 JSON 里），
    #    生产用它等于"预约结果只存在本机" —— 比 Mock 更危险
    #    （Mock 至少不会让人误以为真挂号了）。
    if settings.slot_service_impl.strip().lower() == "local_his":
        problems.append(
            "slot_service_impl='local_his' → 生产禁止使用本地 HIS 替身："
            "号源与挂号单只存在本机，重启即丢，且不具备医院侧事实效力；"
            "生产必须接真实 HIS"
        )

    # 9) 审计（Phase 6）：生产不允许"审计写失败照样执行"，也不允许审计只落容器内
    #    本地文件 —— 容器一重启日志就没了，多实例下各自为政，等于没有审计。
    if not settings.audit_fail_closed:
        problems.append(
            "audit_fail_closed=false → 生产必须开启审计失败关闭："
            "无法留痕的写操作不得执行（审计盘满/不可写时应拒绝，而不是照样挂号）"
        )
    sink = (settings.audit_sink or "").strip().lower()
    if "mongo" not in sink:
        problems.append(
            f"audit_sink={settings.audit_sink!r} → 生产审计必须集中存储（含 mongo）："
            "本地 JSONL 在容器里随重启丢失，多实例也无法统一检索"
        )

    # 10) 知识库灌库（Phase 6）：生产只允许"只读挂载 + 单写者 Job"。
    #     每个实例启动都灌库会在多副本下并发写同一集合，产生半成品索引；
    #     而且索引为空时必须拒绝启动，不能静默降级成"没有医学知识也能答"。
    if settings.rag_ingest_on_startup:
        problems.append(
            "rag_ingest_on_startup=true → 生产必须关闭启动期灌库："
            "灌库应由 scripts/ingest_knowledge.py 单写者 Job 执行（多实例并发写同一集合会产生半成品索引）"
        )
    if not settings.rag_require_index:
        problems.append(
            "rag_require_index=false → 生产必须有索引才能启动："
            "索引为空/不可达时应拒绝启动，而不是降级成『没有知识检索的医疗问答』"
        )

    # 11) 知识条目合规元数据（Phase 7）：来源/审核人/复核日期/失效日期。
    #     医疗内容必须可溯源 —— 出问题时第一个问题是"这条内容谁写的、谁审的、
    #     依据是什么、什么时候到期"。缺这四个字段的条目在法律上不可举证。
    if not settings.rag_require_doc_meta:
        problems.append(
            "rag_require_doc_meta=false → 生产必须校验知识条目元数据："
            "每个知识文件都要在 frontmatter 声明 source / reviewer / reviewed_at / expires_at"
            "（医疗内容需可溯源、可追责，过期条目必须复审）"
        )

    # 12) 预约敏感字段保护（Phase 8-B / 审计 P0-06）：身份证号明文落库
    #     等于把"谁在哪天看了什么病"整表暴露给任何一次拖库。
    #     唯一约束必须建在 HMAC 指纹上（Fernet 随机化加密无法做唯一键）。
    if not settings.pii_protection_enabled:
        problems.append(
            "pii_protection_enabled=false → 生产必须开启预约敏感字段保护："
            "身份证号需以密文落库、查重走 HMAC 指纹（P0-06 明文落库）"
        )
    elif not settings.pii_secret_key.strip():
        problems.append(
            "pii_protection_enabled=true 但 pii_secret_key 为空 → "
            "加密与指纹都需要 Fernet 密钥，生产禁止静默降级成明文"
        )

    if problems:
        raise ProductionConfigError(
            f"APP_ENV=production 启动被拒绝，共 {len(problems)} 项不合规：\n  - "
            + "\n  - ".join(problems)
            + "\n修复后再启动；内网演示/本地开发请保持 APP_ENV=development（默认）。"
        )
