# ColaSeedBox · 可乐PT 保种组（独立版）

不需要 MoviePilot。一个容器，一个配置页，专门做一件事：**按档位把可乐PT 官方种子搬进你自己的 qBittorrent / Transmission，长期保种并向站点上报**。

> **和 MP 插件的区别**
> - 插件版：跑在 MoviePilot 里，能用 MP 的下载器、MP 的整理/刮削
> - **独立版（本仓库）**：只有保种。**不含** MP 的加种后自动整理、刮削、订阅、下载器管理 —— 那些是 MP 的职责
> - 两者的保种逻辑是同一份代码（`app/vendor/colaseedgroup.py` 就是插件 0.3.0 的源码，与 moviepilot-plugin-colaseedgroup 逐字节相同）

---

## 一键启动

```bash
mkdir -p colaseedbox/data && cd colaseedbox
curl -fsSL https://raw.githubusercontent.com/ReelCoreHQ/colaseedbox/main/docker-compose.yml -o docker-compose.yml
docker compose up -d
```

然后打开 **http://你的IP:3006**。

### 或者不用 compose

```bash
docker run -d --name colaseedbox --restart unless-stopped \
  -p 3006:3006 \
  -v $(pwd)/data:/data \
  -e TZ=Asia/Shanghai \
  ghcr.io/reelcorehq/colaseedbox:latest
```

### 连宿主机上的 qB

容器里的 `127.0.0.1` 指的是容器自己。要让容器连宿主机的 qB：

```yaml
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

然后在「自定义下载器」里填 `host.docker.internal:8081,用户名,密码`。

---

## 配置

打开面板，从上到下填：

| 区块 | 填什么 |
|---|---|
| **① 可乐PT API Key** | 在站点「账号 → Key → API Key」（<https://www.colapt.com/account/tracker?tab=api>）新建一把，范围勾**读取**和**下载**，粘进来保存。只放在 `X-API-Key` 请求头里，不再需要账号密码和验证码 |
| **② 下载器** | 每行 `地址,用户名,密码`；Transmission 写 `tr,地址,用户名,密码` |
| **⑤ 保种组** | 档位（2T~20T / 自定义 / 全站官种）、运行间隔、进种限流、下架清理 |
| **⑤-2 限速** | 留空或 0 = 不接管 |

配置保存在 `./data/config.json`，容器重建不丢。

### 档位

| 档位 | 目标体积 |
|---|---|
| 2T / 5T / 10T / 15T / 20T | 2048 / 5120 / 10240 / 15360 / 20480 GiB |
| 自定义 | `100M` `100G` `1T` `1P`，不带单位按 GiB |
| **全站官种** | 有多少保多少，目标跟着站点池子涨 |

**「只能超，不能少」**：插件按最合适的组合挑种子，保证总量 ≥ 目标。

### 运行间隔

只解析两种最常见写法（其余按 1 小时）：

```
0 */6 * * *    每 6 小时
*/15 * * * *   每 15 分钟
```

---

## 每轮做什么

1. 拉站点**官方**资源池（非官方一颗都不碰）
2. 实测客户端里带「可乐保种组」标签的种子覆盖体积
3. 缺口 > 0 → 从池子里挑种子补进来（每颗之间停 0.7 秒，避开站点下载限流）
4. 达标后**动态轮换**：删做种人数最多的 N 颗、补最少的 N 颗
5. **下架清理**：回站核对，已下架的从本地删（默认只删种子保留文件）
6. 把本轮结果上报站点（是否进考核由站点按账号身份判定）

### 读数失败的数学

弱网下一次读不到客户端是常态，**不会**直接把覆盖打成 0：

| 连续失败 | 行为 |
|---|---|
| 1–2 轮 | 沿用上次成功读数，覆盖不变 |
| 3 轮起 | 判为掉线，覆盖如实记 0，日志标明 |

---

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` | 配置页 |
| GET | `/api/config` | 读配置（API Key 只回传「是否已设置」） |
| POST | `/api/config` | 写配置 |
| GET | `/api/status` | 运行状态 + 最近一轮结果 |
| POST | `/api/run` | 立即跑一轮 |
| GET | `/api/health` | 健康检查 |
| GET | `/api/docs` | OpenAPI 文档 |

---

## 数据与备份

```
./data/config.json    配置（含下载器密码，注意权限）
```

建议：`chmod 600 ./data/config.json`，并且不要把这个目录提交到任何仓库。

---

## 常见问题

**面板打得开，但「立即跑一轮」报「还没填可乐PT 的 API Key」**
到站点新建一把勾「读取」「下载」的 API Key，填进「① 可乐PT API Key」，保存后再跑。0.2.0 起不再用账号密码 + 验证码（站点会删掉那套登录入口），旧配置里的账号密码会被忽略。

**提示 API Key 不能用**
401 是 Key 填错、被撤销或过期；403 `api_key_scope_denied` 是缺范围（要「读取」拉官种、核对下架、领上报票据，「下载」取种子）。

**覆盖一直是 0**
看日志里有没有 `读不到种子`。如果是，检查下载器地址/账号，以及容器能不能连到它（`docker exec colaseedbox curl -s http://你的qB:8081`）。

**想用 MP 的整理/刮削**
那用插件版（[moviepilot-plugin-colaseedgroup](https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup)），独立版刻意不含这些。

---

## 许可

MIT。保种逻辑与 MoviePilot 插件同源。
