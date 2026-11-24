#!/usr/bin/env bash
# 把 MoviePilot 插件的执行器逻辑逐字节搬进本仓库（app/seedgroup/），并记下来源 commit。
#
#   scripts/sync-engine.sh [插件仓库目录] [git 引用]
#   默认：../moviepilot-plugin-colaseedgroup  HEAD
#
# 搬的是插件里与 MoviePilot 无关的四个文件：engine.py（对齐规则）、site.py（站点接口）、
# clients.py（下载器适配）、torrent.py（info_hash）。插件的 __init__.py 是 MoviePilot 外壳，不搬；
# 本仓库自己的外壳在 app/（配置、调度、面板、直连下载器）。
#
# 规则：app/seedgroup/ 里这四个文件**只能由本脚本改**。要改逻辑，去插件仓库改、合并，再跑本脚本。
# CI 用 scripts/check_vendor.py 校验它们与 VENDOR.json 记录的来源 commit 逐字节一致。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${1:-$ROOT/../moviepilot-plugin-colaseedgroup}"
REF="${2:-HEAD}"
SUBDIR="plugins.v3/colaseedgroup"
FILES=(engine.py site.py clients.py torrent.py)
DEST="$ROOT/app/seedgroup"

git -C "$SRC" rev-parse --git-dir >/dev/null 2>&1 || { echo "不是 git 仓库：$SRC" >&2; exit 1; }
COMMIT="$(git -C "$SRC" rev-parse --verify "$REF^{commit}")"
if [ "$REF" = "HEAD" ] && [ -n "$(git -C "$SRC" status --porcelain -- "$SUBDIR")" ]; then
  echo "提示：插件仓库 $SUBDIR 有未提交改动；本脚本只取已提交的 ${COMMIT:0:7}" >&2
fi

mkdir -p "$DEST"
for f in "${FILES[@]}"; do
  git -C "$SRC" show "$COMMIT:$SUBDIR/$f" > "$DEST/$f"
done
VERSION="$(git -C "$SRC" show "$COMMIT:$SUBDIR/__init__.py" \
  | sed -nE 's/^PLUGIN_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)".*/\1/p' | head -1)"
[ -n "$VERSION" ] || { echo "读不到插件的 PLUGIN_VERSION" >&2; exit 1; }

python3 - "$DEST" "$COMMIT" "$VERSION" "$SUBDIR" "${FILES[@]}" <<'PY'
import hashlib, json, pathlib, sys
dest, commit, version, subdir, *files = sys.argv[1:]
dest = pathlib.Path(dest)
meta = {
    "source": "ReelCoreHQ/moviepilot-plugin-colaseedgroup",
    "path": subdir,
    "commit": commit,
    "plugin_version": version,
    "files": {f: hashlib.sha256((dest / f).read_bytes()).hexdigest() for f in files},
}
(dest / "VENDOR.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", "utf-8")
PY

echo "已同步 $SUBDIR @ ${COMMIT:0:7}（插件 v$VERSION）→ app/seedgroup/"
echo "接着：更新 SYNC.md 的来源 commit，提交信息里写上来源 commit。"
