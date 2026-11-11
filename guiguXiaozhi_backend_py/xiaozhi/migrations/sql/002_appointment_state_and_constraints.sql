-- 002：预约表状态机、乐观锁与幂等键（Phase 3a / 审计 P0-04）
--
-- 背景：审计 P0-04 指出 appointment 表「无事务、无唯一索引」，
-- 并发下同一预约可以插入多条。本迁移补齐三样东西：
--
--   1. status      —— 预约状态（取消走状态迁移而非物理删除，保留审计痕迹）
--   2. version     —— 乐观锁版本号，防止并发覆盖
--   3. idempotency_key + 唯一约束 —— 并发下同一业务键只能落一条
--
-- MySQL 8.0 不支持 "ADD COLUMN IF NOT EXISTS"，
-- 因此这里用 information_schema 判断后再改（迁移执行器保证整份脚本只跑一次，
-- 所以这里的幂等判断只是双保险）。
--
-- ⚠️ 唯一约束的列必须与**业务查重键严格一致**：
--    工具层 `Appointment.key()` = (username, id_card, department, date, time)
--    —— 五元组，**含 username**。
--    最初只用了 (id_card, department, date, time) 四元组，结果把两个
--    **不同患者**（姓名不同、身份证号恰好相同 —— 测试数据常见）误判为重复，
--    迁移直接撞唯一键失败。**约束比业务键更严比没有约束更糟**：
--    它会拒绝合法业务。
--
-- 生成列 active_marker：只有**未取消**的记录参与唯一约束，
-- 取消后同一时段可以重新预约。

SET @schema := DATABASE();

-- ---- 1. status ----
SET @has_status := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment' AND COLUMN_NAME = 'status'
);
SET @sql := IF(@has_status = 0,
    'ALTER TABLE appointment ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT ''CONFIRMED'' COMMENT ''预约状态''',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- 2. version（乐观锁）----
SET @has_version := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment' AND COLUMN_NAME = 'version'
);
SET @sql := IF(@has_version = 0,
    'ALTER TABLE appointment ADD COLUMN version INT NOT NULL DEFAULT 1 COMMENT ''乐观锁版本号''',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- 3. created_at / updated_at ----
SET @has_created := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment' AND COLUMN_NAME = 'created_at'
);
SET @sql := IF(@has_created = 0,
    'ALTER TABLE appointment ADD COLUMN created_at DATETIME DEFAULT NULL COMMENT ''创建时间''',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_updated := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment' AND COLUMN_NAME = 'updated_at'
);
SET @sql := IF(@has_updated = 0,
    'ALTER TABLE appointment ADD COLUMN updated_at DATETIME DEFAULT NULL COMMENT ''更新时间''',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- 4. active_marker：生成列，唯一约束的参与者 ----
-- 语义：只有**未取消**的预约参与唯一约束；取消后同一时段可以重新预约。
-- 生成列取值：CANCELLED → NULL（NULL 不参与唯一约束），其余 → 1
SET @has_marker := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment' AND COLUMN_NAME = 'active_marker'
);
SET @sql := IF(@has_marker = 0,
    'ALTER TABLE appointment ADD COLUMN active_marker TINYINT GENERATED ALWAYS AS (IF(status = ''CANCELLED'', NULL, 1)) STORED COMMENT ''唯一约束参与者：未取消=1''',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- 5. 唯一约束 uk_appointment_active ----
-- 同一人 + 同科室 + 同日期 + 同时段，只能有一条**未取消**的预约。
-- 这一条把工具层 find_duplicate 与写入之间的竞态窗口关掉。
SET @has_uk := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment'
      AND INDEX_NAME = 'uk_appointment_active'
);
SET @sql := IF(@has_uk = 0,
    'ALTER TABLE appointment ADD UNIQUE KEY uk_appointment_active (username, id_card, department, date, time, active_marker)',
    'DO 0');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- 6. 补齐存量数据的状态字段 ----
UPDATE appointment SET status = 'CONFIRMED' WHERE status IS NULL OR status = '';
