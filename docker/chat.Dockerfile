# 对话服务镜像：构建上下文为项目根目录
# docker build -f docker/chat.Dockerfile -t apec-chat .
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

COPY apec-chat/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY apec-chat/server.py apec-chat/prompt.md ./

EXPOSE 8840
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8840"]
