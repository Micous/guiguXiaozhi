-- 005：审批记录补「可执行参数」（Phase 3b 补充 / 2026-11-11 真机事故）
--
-- 背景（真机事故）：审批弹窗里展示的是**发起轮**模型生成的参数，
-- 用户点确认后，执行轮模型**重新生成**了一遍参数 —— 7b 模型两次输出
-- 不完全一致（实测：发起轮带 doctor_name="蔡柏蔷"，执行轮漏了它），
-- 于是 canonical_args_hash 不匹配 → consume 拒绝 → 用户点了确认却被拒
-- （"审批校验未通过（invalid_or_replayed）"）。
--
-- 正确语义：**用户确认的是弹窗里那组参数，系统就该按那组参数执行**。
-- 所以审批记录需要能还原「发起轮的可执行参数」。
--
-- ⚠️ 安全水位说明：
--   本列存的是**执行所需的真实参数**（含明文身份证号），与 appointment 表
--   同水位。args_preview 仍是脱敏副本，**只用于回显**，本列绝不回显给前端。
--   P0-06（明文身份证）整改时，本列与 appointment.id_card 一并切换到
--   patient_ref（脱敏引用），届时本列存 patient_ref 即可。
--
-- 旧记录（canonical_args 为 NULL）保持原行为：参数不匹配仍拒绝。

SET @schema := DATABASE();

SET @has_canonical := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'approval_requests'
      AND COLUMN_NAME = 'canonical_args'
);
SET @sql_canonical := IF(
    @has_canonical > 0,
    'SELECT 1',
    'ALTER TABLE approval_requests ADD COLUMN canonical_args MEDIUMTEXT NULL AFTER args_preview'
);
PREPARE stmt_canonical FROM @sql_canonical;
EXECUTE stmt_canonical;
DEALLOCATE PREPARE stmt_canonical;
