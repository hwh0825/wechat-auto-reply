# -*- coding: utf-8 -*-
"""BotController：界面对机器人线程的唯一操作入口。

负责：监控线程生命周期（启动/停止/暂停）、运行状态上报、设置文件的读写。
界面只调用本类的方法，不直接碰 monitor 的内部。
"""
import logging
import threading
import time
from pathlib import Path

from . import keys
from .config import (load_config_raw, load_llm_settings, save_config_values,
                     save_llm_settings, _atomic_write_text)
from .guard import Guard
from .llm import build_system_prompt
from .monitor import run_forever
from .persona_presets import DEFAULT_PRESET, PRESETS
from .runtime import RuntimeStatus, acquire_single_instance, release_single_instance
from .state import State


class BotController:
    def __init__(self, status: RuntimeStatus):
        self.status = status
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.bot_state = State()
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------

    @property
    def monitoring(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_monitoring(self):
        """启动监控线程。返回 (ok, 消息)"""
        if self.monitoring:
            return False, "监控已在运行中"
        settings, err = load_llm_settings()
        if settings is None:
            return False, f"请先在【设置 → AI 接口】完成配置（{err}）"
        if not acquire_single_instance():
            return False, "检测到另一个助手实例正在运行，请勿双开"
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        cfg = load_config_raw()
        bot_state = self.bot_state
        guard = Guard(cfg)
        system_prompt = build_system_prompt(_load_persona_text())
        self._thread = threading.Thread(
            target=self._run_bot, args=(cfg, guard, bot_state, system_prompt, settings),
            name="wechat-bot", daemon=True)
        self.status.set_running(True, started_at=time.time())
        self.status.set_last_error("")
        self._thread.start()
        return True, "监控已启动"

    def _run_bot(self, cfg, guard, bot_state, system_prompt, settings):
        try:
            run_forever(cfg, guard, bot_state, system_prompt, "", settings,
                        status=self.status, stop_event=self.stop_event,
                        pause_event=self.pause_event)
        except Exception as e:                      # 线程兜底：任何未捕获异常都不得静默
            logging.exception("监控线程异常退出: %s", e)
            self.status.set_last_error(f"监控线程异常退出: {e}")
        finally:
            self.status.set_running(False)
            release_single_instance()

    def stop_monitoring(self):
        """停止监控线程，最多等 25 秒（一次完整发送流程可能较长）"""
        if not self.monitoring:
            return True, "监控未在运行"
        self.stop_event.set()
        self.pause_event.clear()
        t0 = time.time()
        while self._thread.is_alive() and time.time() - t0 < 25:
            time.sleep(0.3)
        alive = self._thread.is_alive()
        return (not alive), ("监控已停止" if not alive else "停止超时，请查看日志")

    def toggle_pause(self):
        if not self.monitoring:
            return False, "监控未在运行"
        if self.pause_event.is_set():
            self.pause_event.clear()
            return True, "监控已恢复"
        self.pause_event.set()
        return True, "监控已暂停"

    # ---------- 设置读写 ----------

    def load_all_settings(self) -> dict:
        """界面初始化用的完整设置快照"""
        cfg = load_config_raw()
        env = load_llm_settings()[0] or {}
        persona_text = _load_persona_text()
        return {
            "watch_list": cfg.get("watch_list", []),
            "ocr_poll": cfg.get("ocr_poll", 20),
            "sensitive_keywords": cfg.get("sensitive_keywords", []),
            "quiet_hours": cfg.get("quiet_hours", []),
            "max_replies_per_hour": cfg.get("max_replies_per_hour", 0),
            "reply_mode": cfg.get("reply_mode", "multi"),
            "base_url": env.get("base_url", ""),
            "api_key": env.get("api_key", ""),
            "model": env.get("model", ""),
            "persona_text": persona_text,
            "preset": _detect_preset(persona_text),
        }

    def save_watch_list(self, names: list):
        cfg = load_config_raw()
        cfg["watch_list"] = [n.strip() for n in names if n.strip()]
        save_config_values(cfg)
        return True, f"已保存 {len(cfg['watch_list'])} 个监控对象（20 秒内生效）"

    def save_api(self, base_url: str, api_key: str, model: str):
        if not (base_url.strip() and api_key.strip() and model.strip()):
            return False, "三项都要填写"
        save_llm_settings(base_url, api_key, model)
        return True, "AI 接口配置已保存"

    def save_persona(self, text: str):
        from .config import PERSONA_FILE
        _atomic_write_text(PERSONA_FILE, text.strip() + "\n")
        return True, "人设已保存（下一轮回复即生效）"

    def save_advanced(self, ocr_poll: int, sensitive: list, quiet_hours: list,
                      max_replies_per_hour: int, reply_mode: str = None):
        cfg = load_config_raw()
        cfg["ocr_poll"] = ocr_poll
        cfg["sensitive_keywords"] = sensitive
        cfg["quiet_hours"] = quiet_hours
        cfg["max_replies_per_hour"] = max_replies_per_hour
        if reply_mode in ("single", "multi"):
            cfg["reply_mode"] = reply_mode
        save_config_values(cfg)
        return True, "高级设置已保存（20 秒内生效）"

    def wechat_window_exists(self) -> bool:
        return bool(keys.find_wechat_window())


    # ---------- 聊天记录管理 ----------

    def chat_contacts(self) -> list:
        return self.bot_state.chat_contacts()

    def chat_history_text(self, contact: str) -> str:
        mem = self.bot_state.memory_snapshot(contact)
        if not mem:
            return "（暂无聊天记录）"
        return "\n".join(f"{'我' if m['who'] == 'me' else m['who']}: {m['text']}"
                         for m in mem)

    def clear_chat(self, contact: str):
        self.bot_state.clear_memory(contact)
        return True, f"已清空 {contact} 的聊天记录（机器人不再记得之前的对话内容）"

    def clear_all_chats(self):
        self.bot_state.clear_all_memory()
        return True, "已清空全部聊天记录"

    def export_chat(self, filepath: str, contact: str = None):
        """导出聊天记录为文本文件。contact=None 时导出全部联系人。"""
        targets = [contact] if contact else self.bot_state.chat_contacts()
        if contact and contact not in targets:
            return False, f"{contact} 没有聊天记录可导出"
        lines = [f"微信自动回复助手 - 聊天记录导出",
                 f"导出时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 "=" * 40, ""]
        count = 0
        for c in targets:
            mem = self.bot_state.memory_snapshot(c)
            if not mem:
                continue
            lines.append(f"━━━ {c}（{len(mem)} 条）━━━")
            for m in mem:
                who = "我" if m["who"] == "me" else m["who"]
                lines.append(f"{who}: {m['text']}")
            lines.append("")
            count += len(mem)
        if count == 0:
            return False, "没有可导出的聊天记录"
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        Path(filepath).write_text("\n".join(lines), encoding="utf-8")
        return True, f"已导出 {count} 条记录到 {filepath}"

    # ---------- 模块级小工具 ----------

def _load_persona_text() -> str:
    from .config import PERSONA_FILE
    if PERSONA_FILE.exists():
        return PERSONA_FILE.read_text(encoding="utf-8").strip()
    return PRESETS[DEFAULT_PRESET]


def _detect_preset(text: str) -> str:
    for name, preset_text in PRESETS.items():
        if text.strip() == preset_text.strip():
            return name
    return "自定义"
