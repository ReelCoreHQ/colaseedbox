#!/usr/bin/env bash
# 可乐保种组 · Docker 版一键安装（不需要 MoviePilot）
#
#   curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/colaseedgroup-docker/main/install.sh | bash
#
# 可选环境变量：
#   SG_DIR    装到哪（默认 ~/colaseedgroup-docker）
#   SG_PORT   面板端口（默认 3006）
#
# API Key 与下载器在面板里填（http://<本机IP>:3006 → 设置），不经过命令行参数，也不进 shell 历史。
# 幂等：重复跑会更新代码并重建容器，数据目录（$SG_DIR/data）保留。
set -euo pipefail

SG_DIR="${SG_DIR:-$HOME/colaseedgroup-docker}"
SG_PORT="${SG_PORT:-3006}"
TGZ="https://github.com/ReelCoreHQ/colaseedgroup-docker/archive/refs/heads/main.tar.gz"
NAME="colaseedgroup-docker"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31m[错误]\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. 检查环境 ──────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "没装 Docker。先跑：curl -fsSL https://get.docker.com | sh"
docker info >/dev/null 2>&1 || die "Docker 没在跑，或者当前用户没权限（试试 sudo，或把用户加进 docker 组）"
docker compose version >/dev/null 2>&1 || die "缺 docker compose 插件。Debian/Ubuntu：apt install docker-compose-plugin"
case "$SG_PORT" in ''|*[!0-9]*) die "SG_PORT 必须是数字" ;; esac

# ── 2. 取代码 ────────────────────────────────────────────────
say "安装到 $SG_DIR"
mkdir -p "$SG_DIR"
cd "$SG_DIR"
if command -v curl >/dev/null 2>&1; then fetch() { curl -fsSL "$1"; }
elif command -v wget >/dev/null 2>&1; then fetch() { wget -qO- "$1"; }
else die "curl 和 wget 都没有，装一个再来"
fi
say "下载代码"
fetch "$TGZ" | tar xz --strip-components=1 --exclude=data --exclude=.git
mkdir -p data
chmod 700 data
# 容器用普通用户跑：用 data 目录主人的 uid/gid（以 root 跑本脚本时就是 root 建的目录 → 改给 1000:1000）
if [ "$(id -u)" = 0 ]; then chown -R 1000:1000 data; fi
printf 'PUID=%s\nPGID=%s\n' "$(stat -c %u data)" "$(stat -c %g data)" > .env

# ── 3. 端口 ──────────────────────────────────────────────────
if [ "$SG_PORT" != "3006" ]; then
  sed -i "s/\"3006:3006\"/\"$SG_PORT:3006\"/" docker-compose.yml
  say "面板端口改成 $SG_PORT"
fi
# 下载器在宿主机上时，容器里的 127.0.0.1 是容器自己：打开 host.docker.internal
sed -i 's|# extra_hosts:|extra_hosts:|; s|#   - "host.docker.internal:host-gateway"|  - "host.docker.internal:host-gateway"|' docker-compose.yml

# ── 4. 构建并启动 ────────────────────────────────────────────
say "构建镜像（第一次约 1–2 分钟）"
docker compose build --quiet 2>/dev/null || docker compose build
say "启动"
docker compose up -d

# ── 5. 等就绪 ────────────────────────────────────────────────
say "等容器就绪"
ok=
for _ in $(seq 1 40); do
  if docker exec "$NAME" python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:3006/api/health',timeout=3)" >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 2
done
[ -n "$ok" ] || die "容器没起来：docker logs $NAME"

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -z "$IP" ] && IP="<本机IP>"
echo
echo "──────────────────────────────────────────────"
docker ps --filter "name=$NAME" --format '  容器：{{.Names}}  {{.Status}}'
echo "  面板：http://$IP:$SG_PORT"
echo "  数据：$SG_DIR/data（配置含 API Key，权限 0600；重建容器不丢）"
echo "  日志：docker logs -f $NAME"
echo "──────────────────────────────────────────────"
echo
echo "下一步：打开面板 →「设置」→ 填 API Key（站点「账号 → Key → API Key」新建，只勾「保种组」范围）"
echo "        和下载器（qBittorrent / Transmission），保存后会马上跑一轮。"
