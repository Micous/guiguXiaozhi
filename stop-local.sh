#!/usr/bin/env bash
# 停止硅谷小智本地环境（后端 / 前端），保留数据库容器
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/logs"

stop() {
  local name="$1" pidfile="$2" pattern="$3"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    pkill -f "$pattern" 2>/dev/null || true
    rm -f "$pidfile"
    echo "✔ $name 已停止"
  else
    pkill -f "$pattern" 2>/dev/null || true
    echo "· $name 未在运行"
  fi
}

# Python 版后端只按 pid 精确停止，不用 pkill -f "run.py"（会误伤其他 Python 进程）
stop_pid() {
  local name="$1" pidfile="$2"
  if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    kill "$(cat "$pidfile")" 2>/dev/null || true
    rm -f "$pidfile"
    echo "✔ $name 已停止"
  else
    rm -f "$pidfile"
    echo "· $name 未在运行（或由脚本外的进程启动）"
  fi
}

stop "后端" "$LOG_DIR/backend.pid" "spring-boot:run"
stop "后端应用" "$LOG_DIR/backend.pid" "com.atguigu.java.ai.langchain4j.XiaozhiApp"
stop "前端"   "$LOG_DIR/frontend.pid" "vite"
stop_pid "Python 后端" "$LOG_DIR/backend-py.pid"

echo ""
echo "数据库与向量库容器保持运行（需要停止请执行）："
echo "  docker stop guigu-mysql guigu-mongo milvus-standalone milvus-minio milvus-etcd"
