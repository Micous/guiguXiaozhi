-- 007：放宽外部单号列宽（预约永远写不进库的真机事故）
--
-- 背景：2026-11-12 真机事故 —— 用户在确认窗点「确认执行」，服务端确定性执行
-- 真的跑到了最后一步，落库时报：
--
--   1406 (22001): Data too long for column 'external_hold_id' at row 1
--
-- 根因：预占结果不确定（HIS 超时/5xx）时，为了给对账留下唯一凭据，会把
-- **幂等键**写进 external_hold_id，并加 `idem:` 前缀区分语义：
--
--   idem: + sha256(...).hexdigest()   →  5 + 64 = 69 字符
--
-- 而 004 建的列是 VARCHAR(64) —— 69 > 64，**这条路径 100% 必然失败**。
-- 更糟的是它只在"HIS 结果不确定"时才走到，属于最容易出事的时刻：
-- 号源可能已经被占住，本地却因为写不进去而彻底丢失对账线索。
--
-- 为什么是加宽而不是截断：
--   幂等键是对账时向 HIS 反查的唯一凭据（/currentRegister/byKey/{key}）。
--   截断后的键查不到任何东西，等于把"不确定"变成"永远无法收敛"。
--
-- 为什么是 128：
--   ① 现有最长用法 69 字符（idem: + sha256）；
--   ② 真实 HIS 的 holdId / ClinicMaster.id 长度院方各异，128 留出余量；
--   ③ 仍远小于索引键长上限，不引入索引风险。
--
-- 幂等：只在当前长度 < 128 时才 MODIFY，重复执行是 DO 0。

SET @schema := DATABASE();

-- ---- 1. external_hold_id ----
SET @len_hold := (
    SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment'
      AND COLUMN_NAME = 'external_hold_id'
);
SET @sql_hold := IF(
    @len_hold IS NOT NULL AND @len_hold < 128,
    'ALTER TABLE appointment MODIFY COLUMN external_hold_id VARCHAR(128) NULL COMMENT ''HIS 侧预占 id；不确定时存 idem:<幂等键> 作为对账凭据''',
    'DO 0'
);
PREPARE stmt_hold FROM @sql_hold; EXECUTE stmt_hold; DEALLOCATE PREPARE stmt_hold;

-- ---- 2. external_booking_id（同样留余量，防止 confirm 侧再踩一次）----
SET @len_book := (
    SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = @schema AND TABLE_NAME = 'appointment'
      AND COLUMN_NAME = 'external_booking_id'
);
SET @sql_book := IF(
    @len_book IS NOT NULL AND @len_book < 128,
    'ALTER TABLE appointment MODIFY COLUMN external_booking_id VARCHAR(128) NULL COMMENT ''HIS 侧挂号单 id（ClinicMaster.id），对账唯一凭据''',
    'DO 0'
);
PREPARE stmt_book FROM @sql_book; EXECUTE stmt_book; DEALLOCATE PREPARE stmt_book;
