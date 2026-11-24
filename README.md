# 可乐保种组 · Docker 版（colaseedgroup-docker）

> 原名 colaseedbox。

不装 MoviePilot 也能参加可乐PT 保种组：一个小容器，按站点**派单**把官种加进你的 qBittorrent / Transmission 长期做种，
面板在端口 **3006**。

它与 MoviePilot 插件 [moviepilot-plugin-colaseedgroup](https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup)
跑的是**同一份执行器**（`app/seedgroup/`，从插件仓库逐字节搬来，见 [SYNC.md](SYNC.md)），规则完全一致；
本仓库只有外壳：配置、定时运行、直连下载器、面板。用 MoviePilot 的请装插件，不要两个一起对同一个账号跑。

- 站点决定派哪些种子给你（按你在站点申报的可提供空间、做种人少的优先）。
- 每轮拉派单：缺的加、站点**明确收回**的删；上报下载器里这些种子的状态。
- 保种奖励由站点按 tracker 实际观测到的做种情况、按体积计算，**不看这里的上报**。

## 准备

1. 在可乐PT「账号 → Key → API Key」（<https://www.colapt.com/account/tracker?tab=api>）新建一把 Key，只勾 **保种组（seedgroup）** 范围。
2. 在站点保种组页面申报可提供空间（≥ 2 TiB）。
3. 一台 qBittorrent 或 Transmission（可以多台），容器能访问到它的 WebUI / RPC。

## 安装

在要跑保种的机器上：

```bash
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/colaseedgroup-docker/main/install.sh | bash
```

脚本会检查 Docker → 下载代码到 `~/colaseedgroup-docker` → 构建镜像 → 起容器 → 等健康检查通过。
重复跑就是升级，`data/` 里的配置保留。可选：`SG_DIR=/opt/sg SG_PORT=3007 bash install.sh`。

手动：

```bash
git clone --depth 1 https://github.com/ReelCoreHQ/colaseedgroup-docker.git
cd colaseedgroup-docker
mkdir -p data && chmod 700 data
printf 'PUID=%s\nPGID=%s\n' "$(id -u)" "$(id -g)" > .env   # 容器用这个 uid/gid（普通用户）运行
docker compose up -d --build
```

直接用现成镜像（不 clone、不构建；amd64 / arm64 都有）：

```bash
mkdir -p colaseedgroup/data && cd colaseedgroup && chmod 700 data
docker run -d --name colaseedgroup-docker --restart unless-stopped \
  --user "$(id -u):$(id -g)" -p 3006:3006 -v "$PWD/data:/data" \
  ghcr.io/reelcorehq/colaseedgroup-docker:latest
docker logs colaseedgroup-docker 2>&1 | grep 初始登录密码
```

升级：`docker pull ghcr.io/reelcorehq/colaseedgroup-docker:latest`，删掉旧容器后用同一条 `docker run` 再起（`data/` 保留）。
固定版本就把 `latest` 换成版本号，如 `:0.7.1`。

容器**以普通用户运行**（默认 1000:1000，`.env` 里的 `PUID` / `PGID` 可改），不是 root。`data/` 必须归这个 uid/gid 所有；
不可写时容器会在日志里说明并退出。

装好后打开 `http://<本机IP>:3006`，用**初始登录密码**登录（见下面「登录」）→「设置」：填 API Key、下载器，保存后立即跑一轮。

> **从 0.5.x 升级**：0.5.x 用的站点接口已经下线，旧配置（档位、动态保种、`seedgroup_*` 那一套）不再读取。
> 在面板里重新填一遍 API Key（要勾 seedgroup 范围）和下载器即可。旧版加的种子不归新版管，新版不会删它们。

## 设置项

| 设置 | 说明 |
|---|---|
| API Key | 上面那把 Key。保存后只显示「已保存」，接口与面板都不回显 |
| 站点地址 | 默认 `https://www.colapt.com`，一般不用改。**改了地址必须同时重填 Key**，否则旧 Key 会被清掉 |
| 下载器 | 名称（字母开头）、类型、地址、用户名、密码。地址写容器视角，例如 `http://192.168.1.10:8080`；Transmission 默认路径 `/transmission/rpc`。改了地址或用户名必须重填密码 |
| 专用保存目录 | 下载器视角的路径，默认 `/downloads/colaseedgroup`；只放保种组的种子 |
| 专用标签 | 默认「可乐保种组」 |
| 同步间隔 | 默认 30 分钟，最少 10 分钟 |
| 每轮最多加种 / 删种 | 默认 5 / 5，上限 50 / 20 |
| 收回时连文件一起删 | 默认开；内容被别的种子共用（辅种）时只删种子 |

**连宿主机上的下载器**：容器里的 `127.0.0.1` 是容器自己。用宿主机局域网 IP，或在 compose 里打开
`extra_hosts: host.docker.internal:host-gateway`（一键脚本已打开）后写 `http://host.docker.internal:8080`。

## 会做什么、不会做什么

与插件同一套硬规则（不提供开关）：

- **只删自己的**：只有本服务加种成功时记过账的种子，并且它仍在原下载器、仍带专用标签、仍在专用目录下，
  站点在本轮派单里明确收回时才会删除。你改了标签或挪出专用目录，就视为你接手了，不再删它。
- **不接管**：派来的种子如果下载器里本来就有（你自己加的、辅种工具加的），只上报「已存在」，不记账、不删。
- **出错不删**：站点连不上、返回错误或不合规、派单版本倒退、任一下载器列不出种子时，本轮一个都不删。
- **有上限**：每轮加种、删种都有上限；加种失败按 1 小时 / 4 小时 / 1 天退避。
- **不碰别的**：不改任何限速（只读上报），不动派单以外的任何种子（包括别站、馒头的种子）。
- **版本门槛由站点下发**：低于站点要求时只上报、不加不删，面板会提示更新。

## 登录

面板要先登录（一个密码，没有用户名）。

- **初始密码**：第一次启动时随机生成，**只在容器日志里显示这一次**：

  ```bash
  docker logs colaseedgroup-docker 2>&1 | grep 初始登录密码
  ```

  一键脚本装完也会把它打出来。登录后在「设置 → 登录密码」里改成自己的（至少 8 位，要输当前密码）；
  改完其它浏览器的登录全部退出。
- **忘了密码**：删掉 `data/auth.json`，`docker restart colaseedgroup-docker`，日志里会显示一个新的初始密码。
  配置、API Key、账本都不受影响。
- **用环境变量定密码**（可选）：在 compose 的 `environment` 里加 `COLASEEDGROUP_PASSWORD: "至少8位"`，它就是密码，
  面板里不能改，`auth.json` 不再使用。注意环境变量对能跑 `docker inspect` 的人可见，也会留在 compose 文件里。
- 登录保持 7 天（Cookie：HttpOnly、SameSite=Strict）；「退出登录」按钮立即作废；**重启容器需要重新登录**。
- 输错密码：同一来源连错 5 次后要等 2 秒，之后每错一次等待翻倍（最多 15 分钟）；所有来源 10 分钟内合计错 30 次后，
  所有人都要等。等待期间连对的密码也不收。
- **放在 HTTPS 反向代理（Caddy / Nginx）后面**：不用改什么，Cookie 照常工作。环境变量
  `COLASEEDGROUP_COOKIE_SECURE` 控制 Cookie 的 Secure 标记：`auto`（默认，服务自己收到的是 https 才加；
  代理把 https 转成 http 发进来时不加，Cookie 仍会在 https 下发送）、`1`（总是加——之后只能经 https 登录）、`0`（从不加）。
- **反向代理后面的限速**：默认只看直连对端地址，代理后面所有人在服务看来是同一个来源，输错限速等于对所有人一起算
  （一个人连错，别人也要等）。环境变量 `COLASEEDGROUP_TRUSTED_PROXIES`（逗号分隔的 IP / CIDR，IPv4、IPv6 都行）
  告诉服务哪些地址是你的代理：直连对端在列表里时，从右往左读 `X-Forwarded-For`、跳过列表里的地址，第一个不在列表里的就是
  真实客户端；格式不对就退回对端地址。这个地址只用于限速计数，不写日志。
  - **只有真有反向代理挡在前面时才设**。没有代理却设了，任何人都能伪造 `X-Forwarded-For` 换地址绕过单地址限速
    （全局上限仍在）。
  - Caddy / Nginx 和本容器在同一台机器：代理经 docker 网桥进来，写网桥网段，例如 `172.16.0.0/12`。
  - 前面还有 Cloudflare：再加一个 `cloudflare`（展开成 Cloudflare 公布的地址段，写死在 `app/proxies.py`，
    抄录于 2026-11-24；Cloudflare 改了列表就要更新）。转发链最外一跳是 Cloudflare 时，用 Cloudflare 写的
    `CF-Connecting-IP`（像 Caddy 默认那样不信 Cloudflare、只把边缘节点地址写进 `X-Forwarded-For` 时也能拿到真实客户端）。
  - 例：`COLASEEDGROUP_TRUSTED_PROXIES: "172.16.0.0/12,cloudflare"`。有不合法的项时容器在日志里说明并退出。

## 面板

- 上次同步：时间、结果、状态是否已上报；出错时按原因码显示（如 `http_403:api_key_scope_denied`），不显示站点原文。
- 派单条数（保持 / 新派 / 收回）、本服务管理的种子数与总体积、本轮新加 / 删除、申报空间；体积自动换算单位。
- **全部派单**（不只是本服务加的）：种子名 | 体积 | 站点要求 | 本地情况 | 下载器，上方一行摘要
  「派单 · 插件管理 · 非插件管理 · 待加种 · 退避 · 收回」，收回的与有问题的排在前面；文案与 MoviePilot 插件页一致。
  种子名用站点派单里的名字，没有时用下载器里的名字，再没有显示「#种子ID」；不显示 info_hash。
  这里只是下载器一侧的情况，tracker 是否计入以站点保种组页面（/account/seed-group）为准。
- 本轮跳过的原因（已存在不接管、达到上限、退避中……）。
- 「立即同步」按钮：不等定时，马上拉一次派单、对齐下载器、上报状态；顶部显示「自动同步中 · 每 N 分钟」。

面板走普通 HTTP、只有一个密码：最好只在可信的局域网里开放 3006；要从外网访问，放在 HTTPS 反向代理后面，
或把 compose 里的端口改成 `127.0.0.1:3006:3006` 再用 SSH 隧道访问。
面板改不出秘密：Key 与下载器密码永不回显，改站点地址 / 下载器地址都必须同时重填对应的秘密。

## 数据与隐私

数据在 `data/`（容器里的 `/data`），文件权限 0600：

| 文件 | 内容 |
|---|---|
| `config.json` | 站点地址、API Key、实例 ID、下载器（含密码）、目录 / 标签 / 间隔 / 上限 |
| `state.json` | 账本：本服务加过的种子、上次派单版本、加种退避 |
| `last_run.json` | 上一轮结果 |
| `auth.json` | 登录密码的 scrypt 哈希与盐（没有明文）。删掉并重启 = 重置密码 |

日志不写 API Key、下载器密码、下载器地址、签名下载链接，也不写登录令牌和访问者地址；
唯一的例外是第一次启动（或重置后）的那一行初始登录密码。报给站点的只有：派单种子的状态与进度、
每台下载器的限速读数（名字用「类型#序号」）、版本号与实例 ID。

## 常见问题

- **403 `api_key_scope_denied`**：Key 没勾 seedgroup 范围；**403 `permission_denied`**：你还不在保种组。
- **409 `state_conflict`**：还没在站点申报可提供空间。
- **下载器连不上（`unavailable`）**：检查地址是不是容器视角、qB 是否监听 0.0.0.0、用户名密码。
- **种子一直在下载**：保种组的种子要先从别的成员那里下完整才开始做种；保证下载器能连上 tracker。
- **我想退出**：在站点把可提供空间调为 0 或退出保种组，站点会逐步收回派单，这里按每轮上限删除。

## 开发

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
ruff check .
python scripts/check_vendor.py --source ../moviepilot-plugin-colaseedgroup
```

执行器逻辑不在这里改，见 [SYNC.md](SYNC.md)。
