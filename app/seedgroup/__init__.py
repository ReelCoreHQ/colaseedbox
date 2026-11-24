"""从 MoviePilot 插件逐字节搬来的执行器逻辑（engine / site / clients / torrent）。

**不要在这里改代码**：这四个文件只能由 ``scripts/sync-engine.sh`` 从插件仓库同步，
来源 commit 与 sha256 记在 ``VENDOR.json``，CI 用 ``scripts/check_vendor.py`` 校验。
独立版自己的外壳（配置、调度、面板、直连下载器）在上一层 ``app/``。
"""

import json
import pathlib

VENDOR = json.loads((pathlib.Path(__file__).with_name("VENDOR.json")).read_text("utf-8"))
#: 独立版的版本号就是搬来的插件版本：两者跑的是同一份执行器。
VERSION = VENDOR["plugin_version"]
