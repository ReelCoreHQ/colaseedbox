# 与插件同步

执行器逻辑只有一份，在 MoviePilot 插件仓库
[moviepilot-plugin-colaseedgroup](https://github.com/ReelCoreHQ/moviepilot-plugin-colaseedgroup) 的 `plugins.v3/colaseedgroup/`。
本仓库把其中与 MoviePilot 无关的四个文件**逐字节**搬进 `app/seedgroup/`：

| 文件 | 内容 |
|---|---|
| `engine.py` | 一轮对齐：拉派单 → 加 / 删 → 上报（全部硬规则） |
| `site.py` | 站点接口：`getSeedGroupAssignment` / `reportSeedGroupState`，签名下载链接不带 Key |
| `clients.py` | qBittorrent / Transmission 适配（只读限速） |
| `torrent.py` | info_hash |

插件的 `__init__.py`（MoviePilot 外壳）不搬。本仓库自己的外壳在 `app/`：`config.py`（配置与 0600 文件）、
`downloaders.py`（用 MoviePilot 同款库 qbittorrent-api / transmission-rpc 直连下载器，接口与 MoviePilot 的下载器模块同形）、
`runner.py`（跑一轮与调度）、`view.py` + `templates/index.html`（面板）、`main.py`（HTTP）。

## 当前来源

见 `app/seedgroup/VENDOR.json`：来源 commit、插件版本、每个文件的 sha256。
Docker 版的版本号就是这里记的插件版本（当前 **0.7.2**），不单独维护。

## 怎么同步

```bash
scripts/sync-engine.sh ../moviepilot-plugin-colaseedgroup <commit 或分支>   # 默认 HEAD
python scripts/check_vendor.py --source ../moviepilot-plugin-colaseedgroup
python -m unittest discover -s tests -v
```

提交信息写上来源 commit。`app/seedgroup/` 里这四个文件**不许手改**：CI 会检出 VENDOR.json 记的那个 commit 逐字节比对，
改过就不过。要改逻辑，去插件仓库改、合并，再同步过来。
