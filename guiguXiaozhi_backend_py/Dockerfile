# 硅谷小智 · Python 后端（自研 Harness，不依赖 Agent 框架）
#
# 加固要点（Phase 6）：
#   * 非 root 运行（uid/gid 10001）：应用被攻破时不能改容器内文件系统或装东西；
#   * HEALTHCHECK 只探 **liveness**（/health），不探 /ready ——
#     readiness 依赖 Ollama/Milvus/MySQL/Mongo，用 readiness 做健康检查会在
#     依赖抖动时触发无意义的容器重启循环。readiness 交给编排层探 /ready
#     （见 compose.yaml 注释）；
#   * 密钥一律环境注入（不 COPY .env；.dockerignore 已排除）；
#   * 依赖版本全部钉死（requirements.txt），构建可复现。

# 基础镜像可用 --build-arg 覆盖（内网/镜像站/离线环境不必改文件）：
#   docker build --build-arg BASE_IMAGE=<你的镜像站>/library/python:3.11-slim .
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# 依赖先装：代码改动不会让这一层缓存失效
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 非 root 用户；日志目录必须由该用户可写（审计 JSONL / trace 落盘）
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/logs \
    && chown -R app:app /app

USER 10001:10001

EXPOSE 8081

# liveness：进程能应答即可，不查外部依赖
HEALTHCHECK --interval=15s --timeout=4s --start-period=60s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=3).status==200 else 1)"

# 容器内必须监听 0.0.0.0，否则宿主机访问不到
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8081"]
