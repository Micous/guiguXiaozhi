-- 003：任务生命周期持久化（Phase 5 / 审计 P1-02）
--
-- 背景：审计 P1-02 指出引擎的轮次/工具次数/失败计数都是请求内局部状态，
-- 进程退出后无法回答"这个任务执行到哪一步、副作用发生了没有"。
--
-- 四张表（结构与审计 4.x 目标数据结构一致）：
--   agent_tasks       任务（幂等键防重复提交）
--   agent_executions  执行尝试（进程重启后 RUNNING → INTERRUPTED）
--   tool_executions   工具执行（执行前落 STARTED；副作用提交后置 committed）
--   task_events       事件流水（task 内 sequence 单调递增）
--
-- 恢复原则（验收 2）：STARTED 且 side_effect_committed=1 的工具执行，
-- 恢复时**必须先查业务事实**（预约表幂等唯一约束兜底），不可盲目重试。
-- 本迁移只建表；恢复扫描由 TaskRepository.recover_stale 实现。

SET @schema := DATABASE();

-- ---- 1. agent_tasks ----
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id         VARCHAR(64)  NOT NULL PRIMARY KEY,
    subject_id      VARCHAR(64)  NOT NULL,
    session_id      VARCHAR(128) NOT NULL,
    status          VARCHAR(32)  NOT NULL,
    idempotency_key VARCHAR(191) NULL,
    deadline_at     DATETIME     NULL,
    created_at      DATETIME     NOT NULL,
    updated_at      DATETIME     NOT NULL,
    UNIQUE KEY uk_task_idempotency (idempotency_key),
    INDEX idx_task_subject (subject_id),
    INDEX idx_task_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---- 2. agent_executions ----
CREATE TABLE IF NOT EXISTS agent_executions (
    execution_id    VARCHAR(64)  NOT NULL PRIMARY KEY,
    task_id         VARCHAR(64)  NOT NULL,
    attempt         INT          NOT NULL DEFAULT 1,
    runtime_version VARCHAR(32)  NOT NULL DEFAULT '',
    model_id        VARCHAR(64)  NOT NULL DEFAULT '',
    status          VARCHAR(32)  NOT NULL,
    started_at      DATETIME     NOT NULL,
    ended_at        DATETIME     NULL,
    INDEX idx_exec_task (task_id),
    INDEX idx_exec_status (status),
    CONSTRAINT fk_exec_task FOREIGN KEY (task_id) REFERENCES agent_tasks (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---- 3. tool_executions ----
CREATE TABLE IF NOT EXISTS tool_executions (
    tool_execution_id     VARCHAR(64)  NOT NULL PRIMARY KEY,
    execution_id          VARCHAR(64)  NOT NULL,
    task_id               VARCHAR(64)  NOT NULL,
    tool_call_id          VARCHAR(128) NOT NULL,
    tool_name             VARCHAR(128) NOT NULL,
    args_hash             VARCHAR(64)  NOT NULL,
    status                VARCHAR(32)  NOT NULL,
    side_effect_committed TINYINT      NOT NULL DEFAULT 0,
    result_ref            VARCHAR(255) NULL,
    started_at            DATETIME     NOT NULL,
    ended_at              DATETIME     NULL,
    INDEX idx_tool_exec_task (task_id),
    INDEX idx_tool_exec_status (status),
    CONSTRAINT fk_tool_exec_exec FOREIGN KEY (execution_id) REFERENCES agent_executions (execution_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---- 4. task_events ----
CREATE TABLE IF NOT EXISTS task_events (
    event_id    VARCHAR(64)  NOT NULL PRIMARY KEY,
    task_id     VARCHAR(64)  NOT NULL,
    sequence    INT          NOT NULL,
    event_type  VARCHAR(64)  NOT NULL,
    payload_json TEXT        NULL,
    created_at  DATETIME     NOT NULL,
    UNIQUE KEY uk_event_sequence (task_id, sequence),
    CONSTRAINT fk_event_task FOREIGN KEY (task_id) REFERENCES agent_tasks (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
