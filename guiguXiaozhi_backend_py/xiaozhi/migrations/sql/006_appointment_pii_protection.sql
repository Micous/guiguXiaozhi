-- 006：预约表敏感字段保护（Phase 8-B / 审计 P0-06 落库侧）
--
-- 背景：`appointment.id_card` 明文存了两年，而且唯一约束
-- `uk_appointment_active` 直接建在明文列上 —— 这导致"想加密都加不了"：
-- Fernet 是随机化加密，同一身份证号每次密文不同，唯一约束立刻失效。
--
-- 本迁移只做 **expand（加列）**，不动约束、不改存量数据：
--
--   id_card_hash    HMAC-SHA256(身份证号)       确定性，查重/唯一约束用
--   id_card_masked  610121********2831          展示用，免去原件读取
--   id_card         从 VARCHAR(18) 放宽到 255    存 `enc:v1:<fernet token>`
--
-- 为什么约束切换不放在这里：切换前必须先把存量行回填出 hash
-- （HMAC 需要密钥，SQL 算不了），回填与切换都由
-- `scripts/pii_protect_appointments.py` 负责，顺序是
-- expand(本迁移) → backfill(脚本) → contract(脚本切唯一索引)。
-- 把 contract 硬塞进迁移，一旦回填没做就是"唯一约束全空 → 重复预约"。
--
-- 本迁移是**加法**且幂等，未开启 PII 保护时不改变任何行为。

SET @schema := DATABASE();

-- ---- 1. id_card_hash ----
SET @has_hash := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment' AND COLUMN_NAME = 'id_card_hash'
);
SET @sql := IF(@has_hash = 0,
    'ALTER TABLE appointment ADD COLUMN id_card_hash CHAR(64) NULL COMMENT ''身份证号 HMAC-SHA256（查重/唯一约束专用，不可逆）''',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- 2. id_card_masked ----
SET @has_masked := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment' AND COLUMN_NAME = 'id_card_masked'
);
SET @sql := IF(@has_masked = 0,
    'ALTER TABLE appointment ADD COLUMN id_card_masked VARCHAR(32) NULL COMMENT ''身份证号掩码（展示用）''',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- 3. id_card 放宽到 255（存密文）----
SET @id_len := (
    SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment' AND COLUMN_NAME = 'id_card'
);
SET @sql := IF(@id_len IS NOT NULL AND @id_len < 255,
    'ALTER TABLE appointment MODIFY COLUMN id_card VARCHAR(255) NOT NULL COMMENT ''身份证号：明文或 enc:v1: 密文''',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- 4. 指纹索引（查重走它；非唯一，唯一约束由脚本在回填后切换）----
SET @has_hash_idx := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment' AND INDEX_NAME = 'idx_appointment_id_card_hash'
);
SET @sql := IF(@has_hash_idx = 0,
    'ALTER TABLE appointment ADD INDEX idx_appointment_id_card_hash (id_card_hash)',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;
