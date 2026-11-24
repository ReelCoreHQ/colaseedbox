# 可乐保种组 · 独立版（不需要 MoviePilot）
#
# 执行器逻辑 app/seedgroup/ 逐字节搬自 MoviePilot 插件（见 SYNC.md），外壳在 app/。
# 对站点与下载器的请求都走 Python（requests / qbittorrent-api / transmission-rpc），镜像里不装 curl：
# 2026-11 在 python:3.12-slim（OpenSSL 3.5）里实测 requests 直连 www.colapt.com（Cloudflare）握手正常。
FROM python:3.12-slim

LABEL org.opencontainers.image.title="colaseedgroup-docker" \
      org.opencontainers.image.description="可乐PT 保种组独立版：按站点派单保种，与 MoviePilot 插件同一份执行器" \
      org.opencontainers.image.source="https://github.com/ReelCoreHQ/colaseedgroup-docker"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    COLASEEDGROUP_PORT=3006 \
    COLASEEDGROUP_DATA=/data \
    TZ=Asia/Shanghai

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates

# 用普通用户跑（默认 1000:1000）。compose 里用 PUID / PGID 换成宿主机上数据目录主人的 uid/gid。
RUN groupadd --gid 1000 colaseedgroup \
 && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin colaseedgroup \
 && mkdir -p /data && chown 1000:1000 /data && chmod 700 /data
USER 1000:1000

VOLUME ["/data"]
EXPOSE 3006

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3006/api/health',timeout=4).status==200 else 1)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${COLASEEDGROUP_PORT} --no-access-log"]
