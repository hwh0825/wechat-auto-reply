# -*- coding: utf-8 -*-
"""应用运行时：单实例锁、日志初始化、线程安全的运行状态（机器人写、界面读）"""
import logging
import sys
import threading
import time
from contextlib import suppress
from datetime import datetime

import msvcrt

from .config import LOCK_FILE, LOG_DIR

_lock_fh = None


def acquire_single_instance() -> bool:
    """锁定 bot.lock 保证单实例：进程退出/被杀时锁自动释放。"""
    global _lock_fh
    if _lock_fh is not None:
        return False
    try:
        _lock_fh = open(LOCK_FILE, "a+b")
        # msvcrt.locking 从“当前文件指针”开始加锁；a+b 在非空文件上
        # 指针位于末尾，可能让两个进程分别锁住不同字节。统一锁定第 0 字节。
        _lock_fh.seek(0)
        if _lock_fh.read(1) == b"":
            _lock_fh.seek(0)
            _lock_fh.write(b"\0")
            _lock_fh.flush()
        _lock_fh.seek(0)
        msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        with suppress(OSError):
            if _lock_fh is not None:
                _lock_fh.close()
        _lock_fh = None
        return False


def release_single_instance():
    global _lock_fh
    with suppress(Exception):
        if _lock_fh:
            _lock_fh.close()
    _lock_fh = None


def setup_logging(purge_days: int = 30):
    """初始化日志：写文件；无控制台（打包 windowed 模式 sys.stdout 为 None）时不挂屏幕输出。
    同时清理过期旧日志，防止日志目录无限膨胀。"""
    LOG_DIR.mkdir(exist_ok=True)
    cutoff = datetime.now().timestamp() - purge_days * 86400
    for old in LOG_DIR.glob("bot_*.log"):
        with suppress(OSError):
            if old.stat().st_mtime < cutoff:
                old.unlink()
    logfile = LOG_DIR / f"bot_{datetime.now():%Y%m%d}.log"
    handlers = [logging.FileHandler(logfile, encoding="utf-8")]
    if sys.stdout:                      # 打包 windowed 模式下为 None
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers, force=True)
    return logfile


class RuntimeStatus:
    """线程安全的运行状态：机器人线程写，界面线程每秒读快照。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.running = False            # 监控线程存活
        self.paused = False             # 用户暂停
        self.wechat_ok = False          # 最近一次轮询是否找到微信窗口
        self.watch_list: list = []
        self.started_at = 0.0
        self.last_error = ""
        self.recent_replies: list = []  # [(时间戳, 联系人, 回复内容)] 最多 100 条

    def set_running(self, running: bool, started_at: float = None):
        with self._lock:
            self.running = running
            if running and started_at:
                self.started_at = started_at

    def set_paused(self, paused: bool):
        with self._lock:
            self.paused = paused

    def set_wechat_ok(self, ok: bool):
        with self._lock:
            self.wechat_ok = ok

    def set_watch_list(self, watch_list: list):
        with self._lock:
            self.watch_list = list(watch_list)

    def set_last_error(self, msg: str):
        with self._lock:
            self.last_error = msg

    def add_reply(self, contact: str, reply: str):
        with self._lock:
            self.recent_replies.append((time.time(), contact, reply))
            self.recent_replies = self.recent_replies[-100:]

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            today = datetime.now().date()
            return {
                "running": self.running,
                "paused": self.paused,
                "wechat_ok": self.wechat_ok,
                "watch_list": list(self.watch_list),
                "replies_today": sum(1 for ts, _c, _r in self.recent_replies
                                     if datetime.fromtimestamp(ts).date() == today),
                "runtime_min": int((now - self.started_at) / 60) if self.running and self.started_at else 0,
                "recent_replies": list(self.recent_replies[-10:]),
                "last_error": self.last_error,
            }
