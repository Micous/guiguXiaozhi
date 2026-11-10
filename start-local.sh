#!/usr/bin/env bash
# ============================================================
# 硅谷小智 —— 本地一键启动脚本
#
# 依赖：
#   - JDK 17+ / Maven      (后端)
#   - Node.js 18+ / npm    (前端)
#   - Ollama               (本地大模型)
#   - Docker               (MySQL / MongoDB)
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/guiguXiaozhi_backend"
FRONTEND="$ROOT/guiguXiaozhi_frontend"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

export PATH="$HOME/.local/bin:$PATH"

log()  { echo -e "\033[1;36m[$(date '+%H:%M:%S')]\033[0m $1"; }
ok()   { echo -e "\033[1;32m[$(date '+%H:%M:%S')]\033[0m $1"; }
warn() { echo -e "\033[1;33m[$(date '+%H:%M:%S')]\033[0m $1"; }
err()  { echo -e "\033[1;31m[$(date '+%H:%M:%S')]\033[0m $1"; }

# ---------- 守护式启动 ----------
# 后台启动一个长期运行的进程，并让它脱离当前 shell 的进程组，
# 这样无论脚本正常退出还是在终端里被 Ctrl+C，服务都能继续运行。
start_detached() {
  local logfile="$1"; shift
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$logfile" "$@" <<'PYEOF'
import os, subprocess, sys
logfile, cmd = sys.argv[1], sys.argv[2:]
os.setsid()
with open(logfile, 'ab') as f:
    p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
print(p.pid)
PYEOF
  else
    nohup "$@" >> "$logfile" 2>&1 < /dev/null &
    echo $!
  fi
}

# ---------- 1. 基础环境检查 ----------
check_deps() {
  local missing=0
  for cmd in java node npm curl ollama; do
    if command -v "$cmd" >/dev/null 2>&1; then
      ok "✔ $cmd 已安装"
    else
      err "✘ 缺少 $cmd"
      missing=1
    fi
  done
  if ! command -v mvn >/dev/null 2>&1; then
    warn "! 未找到 mvn，请执行: brew install maven  （或解压 Maven 到 ~/tools 并加入 PATH）"
    missing=1
  fi
  [ "$missing" -eq 0 ] || { err "请先补齐上述依赖"; exit 1; }
}

# ---------- 2. Docker 中间件 ----------
start_middleware() {
  if ! docker info >/dev/null 2>&1; then
    err "✘ Docker 未运行，请先启动 Docker Desktop"
    exit 1
  fi

  # MySQL：复用已存在的 mysql 容器，否则新建
  if docker ps -a --format '{{.Names}}' | grep -qx 'guigu-mysql'; then
    docker start guigu-mysql >/dev/null 2>&1 || true
    ok "✔ MySQL 容器 guigu-mysql 已启动"
  elif docker ps --format '{{.Names}}' | grep -qx 'mysql'; then
    ok "✔ 复用已有 MySQL 容器 (mysql)"
  else
    log "正在拉取 MySQL 镜像..."
    docker run -d --name guigu-mysql \
      -e MYSQL_ROOT_PASSWORD=123456 -e MYSQL_DATABASE=guiguxiaozhi \
      -p 3306:3306 mysql:8.0.35 \
      --character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci >/dev/null
    ok "✔ MySQL 容器已创建"
  fi

  # MongoDB
  if docker ps -a --format '{{.Names}}' | grep -qx 'guigu-mongo'; then
    docker start guigu-mongo >/dev/null 2>&1 || true
    ok "✔ MongoDB 容器 guigu-mongo 已启动"
  else
    log "正在拉取 MongoDB 镜像（Docker Hub 不可达时自动改用镜像加速地址）..."
    if ! docker run -d --name guigu-mongo -p 27017:27017 mongo:7 >/dev/null 2>&1; then
      docker run -d --name guigu-mongo -p 27017:27017 docker.m.daocloud.io/library/mongo:7 >/dev/null
    fi
    ok "✔ MongoDB 容器已创建"
  fi

  # Milvus 向量数据库：需要 etcd + minio + milvus 三个容器
  start_milvus() {
    if ! docker ps -a --format '{{.Names}}' | grep -qx 'milvus-standalone'; then
      log "正在创建 Milvus 容器组 (etcd + minio + milvus)..."
      docker network create milvus >/dev/null 2>&1 || true
      docker run -d --name milvus-etcd --network milvus \
        -e ETCD_AUTO_COMPACTION_MODE=revision -e ETCD_AUTO_COMPACTION_RETENTION=1000 \
        -e ETCD_QUOTA_BACKEND_BYTES=4294967296 -e ETCD_SNAPSHOT_COUNT=50000 \
        quay.io/coreos/etcd:v3.5.25 \
        etcd -advertise-client-urls=http://etcd:2379 -listen-client-urls http://0.0.0.0:2379 --data-dir /etcd >/dev/null
      docker run -d --name milvus-minio --network milvus -p 9000:9000 -p 9001:9001 \
        -e MINIO_ACCESS_KEY=minioadmin -e MINIO_SECRET_KEY=minioadmin \
        milvusdb/minio:RELEASE.2024-12-18T13-15-44Z \
        minio server /minio_data --console-address ":9001" >/dev/null
      docker run -d --name milvus-standalone --network milvus -p 19530:19530 -p 9091:9091 \
        -e ETCD_ENDPOINTS=etcd:2379 -e MINIO_ADDRESS=minio:9000 -e MINIO_REGION=us-east-1 \
        milvusdb/milvus:v3.0.3 milvus run standalone >/dev/null
    else
      docker start milvus-etcd milvus-minio >/dev/null 2>&1 || true
      docker start milvus-standalone >/dev/null 2>&1 || true
    fi

    log "正在等待 Milvus 就绪..."
    for i in $(seq 1 60); do
      if curl -s --max-time 3 http://localhost:9091/healthz 2>/dev/null | grep -q "OK"; then
        ok "✔ Milvus 就绪 → localhost:19530"
        return 0
      fi
      sleep 2
    done
    err "✘ Milvus 启动超时，请检查 docker logs milvus-standalone"
    return 1
  }

  # 初始化表结构
  log "正在初始化数据库表结构..."
  local mysql_c
  mysql_c="$(docker ps --format '{{.Names}}' | grep -Ex 'mysql|guigu-mysql' | head -1)"
  for i in $(seq 1 30); do
    docker exec "$mysql_c" mysqladmin ping -uroot -p123456 >/dev/null 2>&1 && break
    sleep 2
  done
  docker exec -i "$mysql_c" mysql -uroot -p123456 >/dev/null 2>&1 <<'SQL' || warn "表结构初始化跳过（可能已存在）"
CREATE DATABASE IF NOT EXISTS guiguxiaozhi DEFAULT CHARACTER SET utf8mb4;
USE guiguxiaozhi;
CREATE TABLE IF NOT EXISTS appointment (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '主键',
    username    VARCHAR(64)  NOT NULL COMMENT '患者姓名',
    id_card     VARCHAR(32)  NOT NULL COMMENT '身份证号',
    department  VARCHAR(64)  NOT NULL COMMENT '预约科室',
    date        VARCHAR(16)  NOT NULL COMMENT '预约日期',
    time        VARCHAR(8)   NOT NULL COMMENT '时段：上午 / 下午',
    doctor_name VARCHAR(64)  DEFAULT NULL COMMENT '医生姓名'
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT ='预约挂号记录表';
SQL
  ok "✔ 数据库表结构就绪"
}

# ---------- 3. Ollama 大模型 ----------
check_ollama() {
  if ! curl -s --max-time 5 http://localhost:11434/api/tags >/dev/null 2>&1; then
    err "✘ Ollama 服务未启动，请执行: ollama serve"
    exit 1
  fi
  ok "✔ Ollama 服务运行中"
  local models
  models="$(curl -s http://localhost:11434/api/tags | grep -o '"name":"[^"]*"' | cut -d'"' -f4)"
  echo "$models" | sed 's/^/    · /'
  echo "$models" | grep -q 'qwen2.5' \
    && ok "✔ 对话模型 qwen2.5 已就绪" \
    || warn "! 未找到 qwen2.5 对话模型，可执行: ollama pull qwen2.5:3b"
  echo "$models" | grep -q 'embedding' \
    && ok "✔ 向量模型已就绪" \
    || warn "! 未找到 embedding 模型，可执行: ollama pull andersc/qwen3-embedding:0.6b"
}

# ---------- 4. 后端 ----------
start_backend() {
  log "正在启动后端 (Spring Boot :8080) ..."
  cd "$BACKEND"

  # 自动选择本机可用的对话模型：优先 qwen2.5:7b，其次 3b / 1.5b
  local models chat_model=""
  models="$(curl -s http://localhost:11434/api/tags | grep -o '"name":"[^"]*"' | cut -d'"' -f4)"
  for candidate in "qwen2.5:7b" "qwen2.5:3b" "qwen2.5:1.5b"; do
    if echo "$models" | grep -qx "$candidate"; then chat_model="$candidate"; break; fi
  done
  local extra_args=()
  if [ -n "$chat_model" ]; then
    ok "使用对话模型：$chat_model"
    extra_args+=("-Dspring-boot.run.arguments=--xiaozhi.ollama.chat-model=$chat_model")
  else
    warn "! 未找到 qwen2.5 系列模型，将使用 application.properties 中的默认配置"
  fi

  : > "$LOG_DIR/backend.log"   # 清空上一次的日志，避免旧内容干扰就绪判断
  local backend_pid
  backend_pid="$(start_detached "$LOG_DIR/backend.log" mvn -B spring-boot:run "${extra_args[@]}")"
  echo "$backend_pid" > "$LOG_DIR/backend.pid"
  for i in $(seq 1 60); do
    if grep -q "Started XiaozhiApp" "$LOG_DIR/backend.log" 2>/dev/null; then
      ok "✔ 后端启动成功 → http://localhost:8080"
      return 0
    fi
    if grep -q "APPLICATION FAILED TO START\|BUILD FAILURE" "$LOG_DIR/backend.log" 2>/dev/null; then
      err "✘ 后端启动失败，日志：$LOG_DIR/backend.log"
      tail -30 "$LOG_DIR/backend.log"
      exit 1
    fi
    sleep 2
  done
  err "✘ 后端启动超时，请查看 $LOG_DIR/backend.log"
  exit 1
}

# ---------- 5. 前端 ----------
start_frontend() {
  log "正在安装前端依赖 ..."
  cd "$FRONTEND"
  [ -d node_modules ] || npm install
  log "正在启动前端 (Vite :5173) ..."
  : > "$LOG_DIR/frontend.log"  # 同上
  local frontend_pid
  frontend_pid="$(start_detached "$LOG_DIR/frontend.log" npm run dev)"
  echo "$frontend_pid" > "$LOG_DIR/frontend.pid"
  for i in $(seq 1 30); do
    if grep -q "ready in\|Local:" "$LOG_DIR/frontend.log" 2>/dev/null; then
      ok "✔ 前端启动成功 → http://localhost:5173"
      return 0
    fi
    sleep 1
  done
  warn "前端启动状态未知，请查看 $LOG_DIR/frontend.log"
}

# ---------- 入口 ----------
echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║        硅谷小智 · 本地环境一键启动            ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

check_deps
start_middleware
check_ollama
start_milvus
start_backend
start_frontend

echo ""
ok "全部服务已启动 🚀"
echo "    前端页面 : http://localhost:5173"
echo "    后端接口 : http://localhost:8080/xiaozhi/chat"
echo "    接口文档 : http://localhost:8080/doc.html"
echo "    日志目录 : $LOG_DIR"
echo ""
echo "停止所有服务请执行: ./stop-local.sh"
echo ""
