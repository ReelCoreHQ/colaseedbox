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

容器**以普通用户运行**（默认 1000:1000，`.env` 里的 `PUID` / `PGID` 可改），不是 root。`data/` 必须归这个 uid/gid 所有；
不可写时容器会在日志里说明并退出。

装好后打开 `http://<本机IP>:3006` →「设置」：填 API Key、下载器，保存后立即跑一轮。

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
| 运行间隔 | 默认 30 分钟，最少 10 分钟 |
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

## 面板

- 上次运行：时间、结果、状态是否已上报；出错时按原因码显示（如 `http_403:api_key_scope_denied`），不显示站点原文。
- 派单条数（保持 / 新派 / 收回）、本服务管理的种子数与总体积、本轮新加 / 删除、申报空间；体积自动换算单位。
- 本服务加的种子列表；本轮跳过的原因（已存在不接管、达到上限、退避中……）。
- 「立即运行」按钮。

面板**没有登录**：请只在可信的局域网里开放 3006，或把 compose 里的端口改成 `127.0.0.1:3006:3006` 再用 SSH 隧道访问。
面板改不出秘密：Key 与下载器密码永不回显，改站点地址 / 下载器地址都必须同时重填对应的秘密。

## 数据与隐私

数据在 `data/`（容器里的 `/data`），文件权限 0600：

| 文件 | 内容 |
|---|---|
| `config.json` | 站点地址、API Key、实例 ID、下载器（含密码）、目录 / 标签 / 间隔 / 上限 |
| `state.json` | 账本：本服务加过的种子、上次派单版本、加种退避 |
| `last_run.json` | 上一轮结果 |

日志不写 API Key、下载器密码、下载器地址、签名下载链接。报给站点的只有：派单种子的状态与进度、
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
