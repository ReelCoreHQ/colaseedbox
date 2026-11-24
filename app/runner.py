"""跑一轮 + 定时调度。

一轮的逻辑全在搬来的 ``seedgroup.engine.Engine`` 里（拉派单 → 对齐 → 上报）；这里只负责
装配（站点客户端、下载器、设置、账本读写）、串行、记下结果给面板看。
"""

import logging
import threading
import time
from collections import Counter
from typing import Any, Callable, Dict, List, Optional

from . import downloaders as downloaders_mod
from .config import Store, missing_fields
from .seedgroup import VERSION
from .seedgroup.engine import Engine, Settings
from .seedgroup.site import Assignment, SiteClient

log = logging.getLogger("colaseedgroup")
USER_AGENT = f"ColaSeedGroup/{VERSION} Docker"
FIRST_RUN_DELAY = 15


class RecordingSiteClient(SiteClient):
    """在插件原文 SiteClient 上只多记一件事：本轮派单按 action 分了多少条（面板用）。"""

    last_counts: Dict[str, int]

    def assignment(self) -> Assignment:
        result = super().assignment()
        self.last_counts = dict(Counter(i.action for i in result.items))
        return result


class Runner:
    def __init__(self, store: Store, build_downloaders: Callable[[List[dict]], List[Any]] = None):
        self.store = store
        self._build = build_downloaders or downloaders_mod.build
        self._lock = threading.Lock()
        self._stop = threading.Event()      # 关停：正在跑的一轮在下一个检查点退出
        self._wake = threading.Event()      # 立即同步 / 配置变了
        self.next_run_at: Optional[float] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def run_cycle(self, trigger: str = "schedule") -> Dict[str, Any]:
        cfg = self.store.load()
        missing = missing_fields(cfg)
        if missing:
            return {"status": "not_configured", "reason": ",".join(missing)}
        if self._stop.is_set():
            return {"status": "stopped"}
        if not self._lock.acquire(blocking=False):
            return {"status": "busy"}
        try:
            site = RecordingSiteClient(cfg["site_url"], cfg["api_key"], cfg["instance_id"], user_agent=USER_AGENT)
            site.last_counts = {}
            engine = Engine(
                site=site,
                downloaders=self._build(cfg["downloaders"]),
                settings=Settings(save_path=cfg["save_path"], tag=cfg["tag"], max_adds=cfg["max_adds"],
                                  max_deletes=cfg["max_deletes"], delete_files=cfg["delete_files"]),
                state=self.store.load_state(),
                persist=self.store.save_state,
                plugin_version=VERSION,
                stop=self._stop,
                log=log,
            )
            report = engine.run().to_dict()
            report["action_counts"] = site.last_counts
        except Exception as err:
            # 兜底：只记异常类型（异常原文可能带地址）
            log.error("[可乐保种组] 本轮异常中止：%s", type(err).__name__)
            report = {"status": "site_error", "reason": f"exception:{type(err).__name__}",
                      "started_at": time.time(), "finished_at": time.time()}
        finally:
            self._lock.release()
        report["trigger"] = trigger
        self.store.save_last_run(report)
        return report

    # ---------------- 调度 ----------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="colaseedgroup-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """立即跑一轮（面板「立即同步」、保存配置后）。"""
        self._wake.set()

    def _loop(self) -> None:
        delay = FIRST_RUN_DELAY
        trigger = "startup"
        while not self._stop.is_set():
            self.next_run_at = time.time() + delay
            if self._wake.wait(delay):
                self._wake.clear()
                trigger = "manual"
            if self._stop.is_set():
                break
            self.next_run_at = None
            try:
                report = self.run_cycle(trigger)
                if report.get("status") == "not_configured":
                    log.info("[可乐保种组] 还没配置好（缺 %s），跳过", report.get("reason"))
            except Exception as err:  # 调度线程不能死
                log.error("[可乐保种组] 调度异常：%s", type(err).__name__)
            delay = self.store.load()["interval"] * 60
            trigger = "schedule"
