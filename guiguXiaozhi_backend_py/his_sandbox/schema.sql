-- ============================================================================
-- HIS 本地沙箱 · 数据库结构
--
-- 领域模型对齐 参考项目/his 的 zp-base / zp-business：
--   sys_dept（科室字典） / pat_master_index（患者主档）
--   clinic_index（号别） → clinic_schedule（周排班） → clinic_for_regist（号表实例）
--   clinic_master（挂号单）
--
-- 与参考项目的**有意差异**（这些差异是修复，不是简化）：
--   1. 号表计数器语义正确：remaining = limitNum - currentNum - currentPreNum。
--      参考项目 updateCurrentNum() 有 off-by-one（limitNum=10 只能挂 9 个），
--      且 currentPreNum 声明后全项目未使用 —— 这里预占是真正的一等公民。
--   2. 补齐 his_hold（预占）、his_idempotency（幂等）、退号状态与号源回补。
--   3. 患者主档不存明文身份证：id_no_hash（HMAC 指纹）+ id_no_masked，
--      对外只暴露 patient_ref，符合本项目 P0-06 的整改方向。
--   4. 所有写操作的并发保护靠 MySQL 行锁 + 条件 UPDATE，不靠进程内锁
--      （独立进程 + 多实例都能防超卖）。
--
-- 本脚本幂等，可重复执行。MySQL 8.0+。
-- ============================================================================

-- ---------- 科室字典（对齐 sys_dept，补上 deptCode 可查） ----------
CREATE TABLE IF NOT EXISTS sys_dept (
  dept_id     VARCHAR(64)  NOT NULL PRIMARY KEY,
  dept_code   VARCHAR(32)  NOT NULL,
  dept_name   VARCHAR(128) NOT NULL,
  org_id      VARCHAR(64)  NOT NULL DEFAULT '',
  status      CHAR(1)      NOT NULL DEFAULT '0',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_dept_code (dept_code),
  UNIQUE KEY uk_dept_name (dept_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='科室/专家字典';

-- ---------- 医生主档（sys_doctor）：科室 + 医生 + 职称 + 擅长 ----------
-- 真实 HIS 由人事/医务主档提供；沙箱从院方导入文件一次性灌入。
-- 应用侧「查询科室专家」工具通过 GET /doctor/list 读这张表，不再读知识库。
CREATE TABLE IF NOT EXISTS sys_doctor (
  doctor_id    VARCHAR(64)  NOT NULL PRIMARY KEY,
  dept_code    VARCHAR(32)  NOT NULL,
  dept_name    VARCHAR(128) NOT NULL,
  doctor_name  VARCHAR(64)  NOT NULL,
  gender       VARCHAR(8)   NOT NULL DEFAULT '',
  title        VARCHAR(64)  NOT NULL DEFAULT '',
  expertise    VARCHAR(512) NOT NULL DEFAULT '',
  status       CHAR(1)      NOT NULL DEFAULT '0',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_doctor_dept (dept_name, doctor_name),
  KEY idx_doctor_dept_code (dept_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='医生主档/专家名录';

-- ---------- 患者主档（对齐 pat_master_index，去明文身份证） ----------
CREATE TABLE IF NOT EXISTS pat_master_index (
  patient_id     VARCHAR(64) NOT NULL PRIMARY KEY,
  patient_ref    VARCHAR(64) NOT NULL,
  id_no_hash     CHAR(64)    NOT NULL,
  id_no_masked   VARCHAR(32) NOT NULL,
  name           VARCHAR(64) NOT NULL,
  phone_masked   VARCHAR(32) NOT NULL DEFAULT '',
  org_id         VARCHAR(64) NOT NULL DEFAULT '',
  del_flag       CHAR(1)     NOT NULL DEFAULT '0',
  created_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_patient_ref (patient_ref),
  UNIQUE KEY uk_id_no_hash (id_no_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='患者主档';

-- ---------- 号别（clinic_index）：科室 + 医生 + 出诊组合 ----------
CREATE TABLE IF NOT EXISTS clinic_index (
  clinic_index_id VARCHAR(64)  NOT NULL PRIMARY KEY,
  dept_id         VARCHAR(64)  NOT NULL,
  dept_name       VARCHAR(128) NOT NULL,
  dept_code       VARCHAR(32)  NOT NULL DEFAULT '',
  doctor_id       VARCHAR(64)  NOT NULL DEFAULT '',
  doctor_name     VARCHAR(64)  NOT NULL,
  org_id          VARCHAR(64)  NOT NULL DEFAULT '',
  created_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_index_doctor (dept_name, doctor_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='号别/排班索引';

-- ---------- 周排班（clinic_schedule） ----------
CREATE TABLE IF NOT EXISTS clinic_schedule (
  clinic_schedule_id VARCHAR(96) NOT NULL PRIMARY KEY,
  clinic_index_id    VARCHAR(64) NOT NULL,
  weekday            TINYINT     NOT NULL COMMENT '0=周一 ... 6=周日',
  time_desc          VARCHAR(16) NOT NULL,
  limit_num          INT         NOT NULL DEFAULT 10,
  limit_pre_num      INT         NOT NULL DEFAULT 0,
  created_at         DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_schedule (clinic_index_id, weekday, time_desc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='医生周出诊安排';

-- ---------- 号表实例（clinic_for_regist）：号源计数器在这里 ----------
CREATE TABLE IF NOT EXISTS clinic_for_regist (
  clinic_for_regist_id VARCHAR(160) NOT NULL PRIMARY KEY,
  clinic_index_id      VARCHAR(64)  NOT NULL,
  clinic_schedule_id   VARCHAR(96)  NOT NULL,
  clinic_date          DATE         NOT NULL,
  time_desc            VARCHAR(16)  NOT NULL,
  limit_num            INT          NOT NULL DEFAULT 10,
  limit_pre_num        INT          NOT NULL DEFAULT 0,
  current_num          INT          NOT NULL DEFAULT 0 COMMENT '已挂号数',
  current_regis_num    INT          NOT NULL DEFAULT 0 COMMENT '当日累计挂号数',
  current_pre_num      INT          NOT NULL DEFAULT 0 COMMENT '已预占数',
  org_id               VARCHAR(64)  NOT NULL DEFAULT '',
  doctor_name          VARCHAR(64)  NOT NULL DEFAULT '',
  dept_name            VARCHAR(128) NOT NULL DEFAULT '',
  del_flag             CHAR(1)      NOT NULL DEFAULT '0',
  created_at           DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uk_slot_day (clinic_schedule_id, clinic_date, time_desc),
  KEY idx_slot_query (dept_name, clinic_date, time_desc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='按日期展开的号表实例';

-- ---------- 预占（参考项目完全缺失） ----------
CREATE TABLE IF NOT EXISTS his_hold (
  hold_id               VARCHAR(64)  NOT NULL PRIMARY KEY,
  clinic_for_regist_id  VARCHAR(160) NOT NULL,
  clinic_master_id      VARCHAR(64)  NOT NULL,
  idempotency_key       CHAR(64)     NOT NULL DEFAULT '',
  status                VARCHAR(16)  NOT NULL DEFAULT 'HELD'
                        COMMENT 'HELD / CONFIRMED / RELEASED / EXPIRED',
  expires_at            DATETIME     NOT NULL,
  created_at            DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at           DATETIME     NULL,
  UNIQUE KEY uk_hold_idem (idempotency_key),
  KEY idx_hold_expire (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='号源预占（带 TTL）';

-- ---------- 挂号单（对齐 clinic_master，补退号状态） ----------
CREATE TABLE IF NOT EXISTS clinic_master (
  clinic_master_id      VARCHAR(64)  NOT NULL PRIMARY KEY,
  clinic_no             VARCHAR(64)  NOT NULL COMMENT '门诊号/单号',
  patient_ref           VARCHAR(64)  NOT NULL,
  patient_id            VARCHAR(64)  NOT NULL DEFAULT '',
  dept_name             VARCHAR(128) NOT NULL DEFAULT '',
  doctor_name           VARCHAR(64)  NOT NULL DEFAULT '',
  clinic_date           DATE         NULL,
  time_desc             VARCHAR(16)  NOT NULL DEFAULT '',
  clinic_for_regist_id  VARCHAR(160) NOT NULL DEFAULT '',
  hold_id               VARCHAR(64)  NOT NULL DEFAULT '',
  idempotency_key       CHAR(64)     NOT NULL DEFAULT '',
  clinic_type           VARCHAR(8)   NOT NULL DEFAULT '1'
                        COMMENT '1 未就诊 / 2 已就诊 / 9 已退号',
  status                VARCHAR(16)  NOT NULL DEFAULT 'REGISTERED'
                        COMMENT 'REGISTERED / CANCELLED',
  created_at            DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  cancelled_at          DATETIME     NULL,
  UNIQUE KEY uk_clinic_no (clinic_no),
  UNIQUE KEY uk_booking_idem (idempotency_key),
  KEY idx_booking_patient (patient_ref, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='门诊挂号单';

-- ---------- 幂等表（跨动作统一，HIS 侧真正保证"同键只处理一次"） ----------
CREATE TABLE IF NOT EXISTS his_idempotency (
  idem_key       CHAR(64)    NOT NULL,
  action         VARCHAR(32) NOT NULL,
  response_json  TEXT        NOT NULL,
  created_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (idem_key, action)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='幂等键 → 首次响应快照';
