# -*- coding: utf-8 -*-
"""运行时状态：预览去重游标、对话记忆、已处理标记、回复频率，落盘 state.json"""
import json
import os
import threading
import time

from .config import STATE_FILE


class State:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {}
        if STATE_FILE.exists():
            try:
                loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._data = loaded
                else:
                    raise ValueError("state.json 顶层必须是对象")
            except (json.JSONDecodeError, OSError, UnicodeError, ValueError) as exc:
                # 状态损坏不应阻止助手启动；保留文件供人工排查，内存从空状态开始。
                import logging
                logging.getLogger(__name__).warning(
                    "state.json 无法读取，已使用空状态: %s", exc)
                self._data = {}

    def _save(self):
        temp = STATE_FILE.with_name(f".{STATE_FILE.name}.tmp")
        try:
            temp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, STATE_FILE)
        except OSError:
            pass  # 落盘失败不影响主流程，内存态仍在
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass

    def entry(self, contact: str) -> dict:
        return self._data.setdefault(contact, {})

    # ---- 对话记忆（每个联系人最近若干条，供 LLM 上下文） ----
    def memory(self, contact: str) -> list:
        with self._lock:
            # LLM 线程只需要读取上下文；返回副本避免界面清记录时读到半更新列表。
            return [dict(item) for item in self.entry(contact).setdefault("memory", [])]

    def remember_message(self, contact: str, who: str, text: str, keep: int = 24):
        with self._lock:
            mem = self.entry(contact).setdefault("memory", [])
            mem.append({"who": who, "text": text})
            self.entry(contact)["memory"] = mem[-keep:]
            self._save()

    # ---- 聊天记录管理（查看/清空/导出） ----
    def chat_contacts(self) -> list:
        """有聊天记忆的联系人列表"""
        with self._lock:
            return [c for c, v in self._data.items()
                    if not c.startswith("_") and v.get("memory")]

    def memory_snapshot(self, contact: str) -> list:
        with self._lock:
            return list(self.entry(contact).get("memory", []))

    def clear_memory(self, contact: str):
        """清空单个联系人的聊天记忆（不影响去重游标）"""
        with self._lock:
            self.entry(contact)["memory"] = []
            self._save()

    def clear_all_memory(self):
        """清空所有联系人的聊天记忆"""
        with self._lock:
            for c, v in self._data.items():
                if not str(c).startswith("_"):
                    v["memory"] = []
            self._save()

    def last_preview(self, contact: str) -> str:
        with self._lock:
            return self.entry(contact).get("last_preview", "")

    def set_last_preview(self, contact: str, preview: str):
        with self._lock:
            self.entry(contact)["last_preview"] = preview
            self._save()

    def handled(self, contact: str) -> bool:
        with self._lock:
            return bool(self.entry(contact).get("handled"))

    def handled_ts(self, contact: str) -> float:
        """上次设为"已处理"的时间戳；0 表示未记录过。
        用于给"同预览未读"的去重加一个冷却窗口，避免把新来的同文本消息漏回。
        """
        with self._lock:
            return float(self.entry(contact).get("handled_ts", 0) or 0)

    def set_handled(self, contact: str, handled: bool, ts: float = None):
        with self._lock:
            entry = self.entry(contact)
            entry["handled"] = handled
            if handled:
                entry["handled_ts"] = ts if ts is not None else time.time()
            else:
                entry["handled_ts"] = 0
            self._save()

    # ---- 单聊常驻模式：已见聊天气泡 ----
    def seen_bubbles(self, contact: str) -> list:
        with self._lock:
            return list(self.entry(contact).get("seen_bubbles", []))

    def set_seen_bubbles(self, contact: str, texts: list):
        """记录已见过的气泡文本（按界面顺序），上限 100 条"""
        with self._lock:
            self.entry(contact)["seen_bubbles"] = [str(t) for t in texts][-100:]
            self._save()

    def last_sent(self, contact: str) -> str:
        """我们最后发出的回复文本（用于识别会话预览里的"自己回显"）"""
        with self._lock:
            return self.entry(contact).get("last_sent", "")

    def set_last_sent(self, contact: str, text: str):
        with self._lock:
            self.entry(contact)["last_sent"] = text
            self._save()

    def record_reply(self, contact: str):
        """记录一次回复时间，用于每小时限速"""
        with self._lock:
            times = self.entry(contact).setdefault("reply_times", [])
            now = time.time()
            times[:] = [t for t in times if now - t < 3600]
            times.append(now)
            self._save()

    def replies_in_last_hour(self, contact: str) -> int:
        with self._lock:
            times = list(self.entry(contact).get("reply_times", []))
            now = time.time()
            return sum(1 for t in times if now - t < 3600)
