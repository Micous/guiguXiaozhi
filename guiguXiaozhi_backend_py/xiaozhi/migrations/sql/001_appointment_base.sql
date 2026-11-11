-- 001：预约表基础结构（仅新库需要）
--
-- 背景：原实现用「启动脚本临时建表」，建表语句散落在各仓储里。
-- 存量库（已有数据）由 002 做增量 ALTER，本文件只负责**空库**首次建表。
--
-- 注意：MySQL 不支持 "ADD COLUMN IF NOT EXISTS"，
-- 所以增量变更统一放在 002，由迁移执行器按版本号保证只跑一次。

CREATE TABLE IF NOT EXISTS appointment (
    id BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
    username VARCHAR(64) NOT NULL COMMENT '患者姓名',
    id_card VARCHAR(32) NOT NULL COMMENT '身份证号',
    department VARCHAR(64) NOT NULL COMMENT '预约科室',
    date VARCHAR(16) NOT NULL COMMENT '预约日期',
    time VARCHAR(8) NOT NULL COMMENT '时段：上午 / 下午',
    doctor_name VARCHAR(64) DEFAULT NULL COMMENT '医生姓名',
    PRIMARY KEY (id),
    INDEX idx_patient (id_card, date),
    INDEX idx_department_date (department, date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预约挂号记录表';
