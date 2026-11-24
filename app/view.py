"""面板数据：把上一轮结果与账本整理成页面要显示的东西（文案、体积换算、错误码说明）。

与插件页（plugins.v3/colaseedgroup/__init__.py 的 get_page）同口径：上次运行结果、派单条数、
本服务管理的种子与体积、本轮新加/删除、申报空间，以及**全部派单**的表（种子名 | 体积 | 站点要求 |
本地情况 | 下载器）。派单表的行、顺序、文案都来自搬来的执行器（``seedgroup.engine``），与插件页逐字一致；
不显示 info_hash。体积一律自动换算单位。
错误只按固定原因码 / 站点的 ``code`` 显示，从不显示站点 ``message`` 或异常原文。
"""

import time
from collections import Counter
from typing import Any, Dict, List, Optional

from .seedgroup.engine import (
    FILTERS,
    ROW_ACTION_TEXT,
    action_text,
    default_filter,
    local_text,
    row_actions,
    row_filter,
    summary_text,
    takeover_warning,
)

STATUS_TEXT = {
    "ok": "正常",
    "site_error": "站点请求失败，本轮未加未删",
    "outdated": "版本低于站点要求，本轮只上报",
    "no_downloader": "没有可用的下载器",
    "stopped": "已中止（服务关停）",
    "not_configured": "未配置",
    "busy": "上一次同步还没结束",
}

#: 原因码（执行器 / 站点客户端 / 下载器给出的固定码）
REASON_TEXT = {
    "http_400": "请求被站点拒绝",
    "http_401": "API Key 无效",
    "http_403": "API Key 没有 seedgroup 权限，或你不在保种组",
    "http_404": "站点没有这个接口（站点版本不对？）",
    "http_409": "还没在站点申报可提供空间",
    "http_413": "上报内容太大",
    "http_429": "请求太频繁",
    "http_5xx": "站点暂时出错",
    "timeout": "连接站点超时",
    "network": "连不上站点",
    "bad_json": "站点返回格式错误",
    "bad_shape": "站点返回的派单不合规",
    "redirect": "站点返回了重定向",
    "bad_download_url": "下载链接不合规",
    "torrent_too_large": "种子文件过大",
    "bad_torrent": "种子文件无效",
    "hash_mismatch": "种子与派单的 info_hash 不符",
    "not_visible": "下载器说加上了却查不到",
    "duplicate": "下载器里已有同一颗",
    "add_failed": "下载器加种失败",
    "unavailable": "下载器连不上",
    "list_failed": "下载器列种子失败",
    "limits_failed": "读不到下载器限速",
    "revision_regressed": "派单版本倒退",
    "client_unavailable": "有下载器不可用",
    "no_downloader": "没有可用的下载器",
    "config_broken": "配置文件损坏",
    "enabled": "服务已停用",
    "api_key": "还没填 API Key",
    "downloaders": "还没配置下载器",
}

#: 站点错误信封里的机器码 code
SITE_CODE_TEXT = {
    "authentication_required": "没有带上有效的 API Key",
    "invalid_credentials": "API Key 无效或已撤销",
    "api_key_scope_denied": "API Key 没勾「保种组（seedgroup）」范围",
    "permission_denied": "你不在保种组",
    "password_change_required": "账号需要先改密码",
    "state_conflict": "还没在站点申报可提供空间",
    "invalid_request": "请求格式不对",
    "rate_limited": "请求太频繁",
    "payload_too_large": "上报内容太大",
    "not_found": "接口不存在",
}

#: 跳过原因（skipped 里的值，冒号前那段）
SKIP_TEXT = {
    "not_owned": "收回的不是本服务加的：不删",
    "external": "下载器里本来就有：不接管",
    "already_gone": "已经不在下载器里：销账",
    "detached": "被挪出专用目录或去掉标签：视为你接手",
    "downloader_not_selected": "记录的下载器已不在配置里",
    "delete_cap": "达到每轮删除上限，下轮再删",
    "add_cap": "达到每轮加种上限，下轮再加",
    "remove_failed": "删除失败",
    "remove_blocked": "删种闸门关闭",
    "backoff": "加种失败退避中",
    "add_failed": "加种失败",
    "no_download_url": "派单没有下载链接",
}

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_size(num: Any) -> str:
    """字节数自动换算单位（与插件页同一规则）。"""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "-"
    value = max(0.0, value)
    unit = 0
    while value >= 1024 and unit < len(_UNITS) - 1:
        value /= 1024
        unit += 1
    if unit == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {_UNITS[unit]}" if value < 100 else f"{value:.0f} {_UNITS[unit]}"


def explain(code: str) -> Dict[str, str]:
    """'http_403:api_key_scope_denied' → {code, text}；只查表，不信任任何自由文本。"""
    code = str(code or "")
    head, _, tail = code.partition(":")
    text = REASON_TEXT.get(head) or SKIP_TEXT.get(head) or head
    if tail in SITE_CODE_TEXT:
        # 站点给了机器码：它比 HTTP 状态更具体，直接用它的说明
        text = SITE_CODE_TEXT[tail]
    elif tail:
        text = f"{text}（{REASON_TEXT.get(tail) or tail}）"
    return {"code": code, "text": text}


def _clock(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def assignments(last: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """派单表的行（已按执行器的顺序排好）。只给显示要用的字段与可做的操作（按 torrent_id）：
    没有 info_hash、没有链接。"""
    rows = last.get("assignments")
    if rows is None:
        return None
    return [{
        "torrent_id": r.get("torrent_id"),
        "name": str(r.get("name") or ""),
        "size_text": human_size(r.get("size") or 0),
        "action": r.get("action") or "",
        "action_text": action_text(r),
        "local": r.get("local") or "",
        "local_text": local_text(r, _clock),
        "downloader": r.get("downloader") or "",
        "filter": row_filter(r),
        "ops": [{"action": "resume" if a == "recheck" else a, "text": ROW_ACTION_TEXT[a]} for a in row_actions(r)],
    } for r in rows]


def _errors(last: Dict[str, Any]) -> List[Dict[str, str]]:
    out = []
    if last.get("status") not in (None, "ok") and last.get("reason") and last.get("status") != "outdated":
        out.append({"where": "站点", **explain(last["reason"])})
    if last.get("state_error"):
        out.append({"where": "状态上报", **explain(last["state_error"])})
    for item in last.get("errors") or []:
        name, _, code = str(item).partition(":")
        out.append({"where": f"下载器 {name}", **explain(code)})
    if last.get("delete_block") and last.get("status") == "ok":
        out.append({"where": "删种闸门", **explain(last["delete_block"])})
    return out


def status(cfg: Dict[str, Any], state: Dict[str, Any], last: Dict[str, Any], *, version: str,
           running: bool, next_run_at: Any, missing: List[str]) -> Dict[str, Any]:
    owned = state.get("owned") or {}
    total = sum(int(rec.get("size") or 0) for rec in owned.values())
    skipped = Counter(str(v).split(":", 1)[0] for v in (last.get("skipped") or {}).values())
    stat = last.get("status")
    summary = None
    if last:
        summary = {
            "status": stat,
            "status_text": STATUS_TEXT.get(stat, stat),
            "trigger": last.get("trigger"),
            "finished_at": last.get("finished_at") or last.get("started_at"),
            "revision": last.get("revision"),
            "items": last.get("items"),
            "action_counts": last.get("action_counts") or {},
            "capacity_gib": last.get("capacity_gib"),
            "capacity_text": human_size(int(last["capacity_gib"]) * 1024 ** 3) if last.get("capacity_gib") else "-",
            "added": len(last.get("added") or []),
            "removed": len(last.get("removed") or []),
            "skipped": [{"code": k, "count": n, "text": SKIP_TEXT.get(k, k)} for k, n in sorted(skipped.items())],
            "deletes_allowed": bool(last.get("deletes_allowed")),
            "state_reported": bool(last.get("state_reported")),
            "min_plugin_version": last.get("min_plugin_version") or "",
            "outdated": stat == "outdated",
            "errors": _errors(last),
        }
    return {
        "version": version,
        "now": time.time(),
        "running": running,
        "next_run_at": next_run_at,
        "interval": cfg.get("interval"),
        "configured": not missing,
        "missing": [{"code": m, "text": REASON_TEXT.get(m, m)} for m in missing],
        "last": summary,
        "assignments": assignments(last),
        "filters": [{"key": k, "text": t} for k, t in FILTERS],
        "default_filter": default_filter(last.get("assignments") or []),
        "takeover_warning": takeover_warning(bool(cfg.get("delete_files", True))),
        "assignment_summary_text": summary_text(last.get("assignment_summary") or {}),
        "assignments_truncated": int(last.get("assignments_truncated") or 0),
        "owned_count": len(owned),
        "owned_total": total,
        "owned_total_text": human_size(total),
    }
