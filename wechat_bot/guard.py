# -*- coding: utf-8 -*-
"""安全护栏：敏感消息拦截、静默时段、限速、界面操作节流、熔断"""
import threading
import time
from datetime import datetime


class Guard:
    def __init__(self, cfg):
        self.cfg = cfg
        self._ui_lock = threading.Lock()
        self._last_ui_action = 0.0
        self.consecutive_errors = 0

    # ---- 界面操作节流：保证任意两次微信 UI 操作间隔 >= min_ui_interval ----
    def ui_pace(self):
        with self._ui_lock:
            wait = self.cfg["min_ui_interval"] - (time.time() - self._last_ui_action)
            if wait > 0:
                time.sleep(wait)
            self._last_ui_action = time.time()

    # ---- 敏感消息：命中则不自动回复，转人工 ----
    def is_sensitive(self, text: str) -> bool:
        return any(kw in text for kw in self.cfg["sensitive_keywords"])

    # ---- 静默时段（支持跨零点，如 ["23:00","07:30"]）----
    def in_quiet_hours(self) -> bool:
        qh = self.cfg.get("quiet_hours") or []
        if not qh or len(qh) != 2:
            return False
        start, end = qh[0], qh[1]
        now = datetime.now().strftime("%H:%M")
        if start <= end:
            return start <= now < end
        return now >= start or now < end   # 跨零点

    # ---- 频率限制（max_replies_per_hour <= 0 表示不限制）----
    def over_rate_limit(self, state, contact: str) -> bool:
        limit = self.cfg["max_replies_per_hour"]
        if limit <= 0:
            return False
        return state.replies_in_last_hour(contact) >= limit

    # ---- 熔断 ----
    def tick_error(self) -> bool:
        """记录一次错误；返回 True 表示已熔断（应停止运行）"""
        self.consecutive_errors += 1
        return self.consecutive_errors >= self.cfg["max_consecutive_errors"]

    def tick_ok(self):
        self.consecutive_errors = 0
