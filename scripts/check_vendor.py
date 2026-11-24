"""校验 app/seedgroup/ 里搬来的插件文件没有被改过。

    python scripts/check_vendor.py                 # 对 VENDOR.json 里记的 sha256
    python scripts/check_vendor.py --source DIR    # 再与插件仓库逐字节比对

``--source`` 可以是插件仓库的 git 目录（按 VENDOR.json 的 commit 用 ``git show`` 取文件），
也可以是已经检出到那个 commit 的普通目录（CI 用 actions/checkout 检出的就是这种）。
"""

import argparse
import hashlib
import json
import pathlib
import subprocess  # nosec B404 - 只调本机 git 读文件
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "app" / "seedgroup"


def _source_bytes(src: pathlib.Path, commit: str, rel: str) -> bytes:
    plain = src / rel
    is_git = (src / ".git").exists()
    if is_git:
        head = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],  # nosec B603 B607
                              capture_output=True, text=True, check=True).stdout.strip()
        if head != commit:
            return subprocess.run(["git", "-C", str(src), "show", f"{commit}:{rel}"],  # nosec B603 B607
                                  capture_output=True, check=True).stdout
    return plain.read_bytes()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=pathlib.Path, help="插件仓库目录")
    args = ap.parse_args()

    meta = json.loads((VENDOR / "VENDOR.json").read_text("utf-8"))
    commit, subdir, files = meta["commit"], meta["path"], meta["files"]
    bad = []
    for name, want in files.items():
        data = (VENDOR / name).read_bytes()
        got = hashlib.sha256(data).hexdigest()
        if got != want:
            bad.append(f"{name}: sha256 与 VENDOR.json 不符（被手改过？改逻辑请去插件仓库）")
        if args.source:
            theirs = _source_bytes(args.source, commit, f"{subdir}/{name}")
            if theirs != data:
                bad.append(f"{name}: 与插件仓库 {commit[:7]} 的 {subdir}/{name} 不一致")
    extra = sorted(p.name for p in VENDOR.glob("*.py") if p.name not in files and p.name != "__init__.py")
    if extra:
        bad.append(f"app/seedgroup/ 里有不是搬来的文件：{', '.join(extra)}（外壳代码放 app/）")
    if bad:
        print("\n".join(bad), file=sys.stderr)
        return 1
    where = f"，并与 {args.source} 逐字节一致" if args.source else ""
    print(f"vendor ok: {len(files)} 个文件 @ {commit[:7]}（插件 v{meta['plugin_version']}）{where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
