# 独立版保种组：不需要 MoviePilot，只对接站点与你自己的 qB/TR。
FROM python:3.12-slim

LABEL org.opencontainers.image.title="ColaSeedBox" \
      org.opencontainers.image.description="可乐PT 保种组独立服务：按档位把站点官方种搬进下载器长期保种" \
      org.opencontainers.image.source="https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup"

ENV PYTHONUNBUFFERED=1 \
    COLASEEDBOX_PORT=3006 \
    COLASEEDBOX_CONFIG=/data/config.json \
    TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates

# 配置与运行时状态都落在 /data，容器重建不丢
VOLUME ["/data"]
EXPOSE 3006

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3006/api/health',timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "3006"]
