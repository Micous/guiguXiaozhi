-- 004：预约记录补 HIS 对接字段（Phase 3b / 审计 P0-03）
--
-- 背景：Phase 3a 的预约表只记"本地业务事实"（谁、哪科、哪天、哪个时段），
-- 没有**外部单号**。真实 HIS 接入后，对账（UNKNOWN → CONFIRMED/FAILED）
-- 必须知道"该问 HIS 哪个单号"，否则 UNKNOWN 记录永远无法收敛。
--
-- 新增字段：
--   external_booking_id —— HIS 侧挂号单 id（ClinicMaster.id），对账的唯一凭据
--   external_hold_id    —— HIS 侧预占 id，释放/确认时使用
--   patient_ref         —— 患者引用（脱敏后的内部引用），**替代明文身份证做业务键**
--   reconciled_at       —— 最后一次对账完成时间（NULL = 从未对账）
--   reconcile_attempts  —— 对账尝试次数（连续失败达阈值后告警）
--
-- ⚠️ patient_ref 是 P0-06 的整改方向：真实上线后业务主键应该用
--    patient_ref（不可逆的脱敏引用），而不是明文身份证号。
--    本迁移只加字段，**不删 id_card** —— 存量数据与既有查重键仍依赖它，
--    真正切换业务键需要配套迁移（见差异清单第 11 节残留）。

SET @schema := DATABASE();

-- ---- 1. external_booking_id ----
SET @has_ebb := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment'
      AND COLUMN_NAME = 'external_booking_id'
);
SET @sql_ebb := IF(
    @has_ebb > 0,
    'SELECT 1',
    'ALTER TABLE appointment ADD COLUMN external_booking_id VARCHAR(64) NULL AFTER doctor_name'
);
PREPARE stmt_ebb FROM @sql_ebb; EXECUTE stmt_ebb; DEALLOCATE PREPARE stmt_ebb;

-- ---- 2. external_hold_id ----
SET @has_eh := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment'
      AND COLUMN_NAME = 'external_hold_id'
);
SET @sql_eh := IF(
    @has_eh > 0,
    'SELECT 1',
    'ALTER TABLE appointment ADD COLUMN external_hold_id VARCHAR(64) NULL AFTER external_booking_id'
);
PREPARE stmt_eh FROM @sql_eh; EXECUTE stmt_eh; DEALLOCATE PREPARE stmt_eh;

-- ---- 3. patient_ref ----
SET @has_pr := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment'
      AND COLUMN_NAME = 'patient_ref'
);
SET @sql_pr := IF(
    @has_pr > 0,
    'SELECT 1',
    'ALTER TABLE appointment ADD COLUMN patient_ref VARCHAR(128) NULL AFTER id_card'
);
PREPARE stmt_pr FROM @sql_pr; EXECUTE stmt_pr; DEALLOCATE PREPARE stmt_pr;

-- ---- 4. reconciled_at / reconcile_attempts ----
SET @has_rec := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment'
      AND COLUMN_NAME = 'reconciled_at'
);
SET @sql_rec := IF(
    @has_rec > 0,
    'SELECT 1',
    'ALTER TABLE appointment ADD COLUMN reconciled_at DATETIME NULL AFTER status'
);
PREPARE stmt_rec FROM @sql_rec; EXECUTE stmt_rec; DEALLOCATE PREPARE stmt_rec;

SET @has_att := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment'
      AND COLUMN_NAME = 'reconcile_attempts'
);
SET @sql_att := IF(
    @has_att > 0,
    'SELECT 1',
    'ALTER TABLE appointment ADD COLUMN reconcile_attempts INT NOT NULL DEFAULT 0 AFTER reconciled_at'
);
PREPARE stmt_att FROM @sql_att; EXECUTE stmt_att; DEALLOCATE PREPARE stmt_att;

-- ---- 5. 索引：对账扫描与外部单号查询 ----
SET @has_idx_ebb := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment'
      AND INDEX_NAME = 'idx_appointment_external_booking'
);
SET @sql_idx_ebb := IF(
    @has_idx_ebb > 0,
    'SELECT 1',
    'ALTER TABLE appointment ADD INDEX idx_appointment_external_booking (external_booking_id)'
);
PREPARE stmt_idx_ebb FROM @sql_idx_ebb; EXECUTE stmt_idx_ebb; DEALLOCATE PREPARE stmt_idx_ebb;

SET @has_idx_status := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment'
      AND INDEX_NAME = 'idx_appointment_status'
);
SET @sql_idx_status := IF(
    @has_idx_status > 0,
    'SELECT 1',
    'ALTER TABLE appointment ADD INDEX idx_appointment_status (status)'
);
PREPARE stmt_idx_status FROM @sql_idx_status; EXECUTE stmt_idx_status; DEALLOCATE PREPARE stmt_idx_status;

-- ---- 6. active_marker 语义扩展：FAILED 也算终态，不再占用唯一约束 ----
--
-- 背景：Phase 3b 新增 FAILED 状态（对账确认 HIS 侧确实没有这条单）。
-- FAILED 是**终态** —— 用户应该能重新约同一时段。如果 active_marker
-- 仍把 FAILED 算作"活跃"，唯一约束会拒绝重新预约（用户视角：
-- "明明失败了我却约不上了"）。
--
-- ⚠️ 生成列的表达式变了 → 必须 DROP 再重建（MySQL 不允许改生成列表达式）。
--    先删唯一键，再改生成列，最后重建唯一键 —— 顺序不能乱，否则
--    重建唯一键时旧列还在会报重复索引。
--
-- ⚠️ DDL 不可回滚：重建前若表里已有 FAILED 记录，重建唯一键时它们
--    全部是 NULL（生成列）→ 不参与约束 → 不会冲突。

SET @has_marker := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment'
      AND COLUMN_NAME = 'active_marker'
);
SET @sql_marker := IF(
    @has_marker > 0,
    'ALTER TABLE appointment DROP INDEX uk_appointment_active',
    'SELECT 1'
);
PREPARE stmt_marker FROM @sql_marker; EXECUTE stmt_marker; DEALLOCATE PREPARE stmt_marker;

SET @sql_drop_marker := IF(
    @has_marker > 0,
    'ALTER TABLE appointment DROP COLUMN active_marker',
    'SELECT 1'
);
PREPARE stmt_drop_marker FROM @sql_drop_marker; EXECUTE stmt_drop_marker; DEALLOCATE PREPARE stmt_drop_marker;

ALTER TABLE appointment
    ADD COLUMN active_marker TINYINT GENERATED ALWAYS AS (
        IF(status IN ('CANCELLED', 'FAILED'), NULL, 1)
    ) STORED COMMENT '唯一约束参与者：未取消且未失败=1';

ALTER TABLE appointment
    ADD UNIQUE KEY uk_appointment_active (username, id_card, department, date, time, active_marker);
