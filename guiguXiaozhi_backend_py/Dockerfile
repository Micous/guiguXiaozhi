FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8081

# 容器内必须监听 0.0.0.0，否则宿主机访问不到
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8081"]
