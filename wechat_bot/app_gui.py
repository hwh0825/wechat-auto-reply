# -*- coding: utf-8 -*-
"""微信自动回复助手图形界面（CustomTkinter 四页：状态 / 设置 / 聊天记录 / 日志）"""
import logging
import os
import queue
from datetime import datetime

import customtkinter as ctk

from .config import load_llm_settings, LOG_DIR
from .controller import BotController
from .persona_presets import DEFAULT_PRESET, PRESETS
from .runtime import RuntimeStatus, setup_logging
from .tray import TrayIcon

API_PRESETS = {
    "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "智谱 GLM": ("https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
    "通义千问": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
    "Moonshot Kimi": ("https://api.moonshot.cn/v1", "moonshot-v1-8k"),
    "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "自定义": ("", ""),
}

GREEN, RED, ORANGE, GRAY = "#2fa572", "#da4453", "#e6a23c", "#8a8a8a"


class AssistantApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.title("微信自动回复助手")
        self.geometry("920x660")
        self.minsize(840, 580)

        self.controller = BotController(RuntimeStatus())
        self.settings = self.controller.load_all_settings()
        self._actions: queue.Queue = queue.Queue()
        self.tray = TrayIcon(self._actions)
        self._tray_ok = self.tray.start()
        self._log_cache = ""
        self._tick = 0
        self._force = False

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_topbar()
        self._build_tabs()
        self._load_settings_to_ui()
        self.after(1000, self._refresh_loop)
        self.after(150, self._drain_actions)

    # ================= UI 构建 =================

    def _build_topbar(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(bar, text="微信自动回复助手", font=("微软雅黑", 20, "bold")).pack(side="left")
        self.wx_dot = ctk.CTkLabel(bar, text="● 未连接微信", text_color=GRAY,
                                   font=("微软雅黑", 12))
        self.wx_dot.pack(side="right", padx=(0, 12))
        self.pause_btn = ctk.CTkButton(bar, text="暂停", width=80, state="disabled",
                                       command=self._on_pause)
        self.pause_btn.pack(side="right", padx=6)
        self.start_btn = ctk.CTkButton(bar, text="启动监控", width=110,
                                       command=self._on_start_stop)
        self.start_btn.pack(side="right")

        self.banner = ctk.CTkLabel(self, text="", text_color=ORANGE,
                                   font=("微软雅黑", 12), anchor="w")
        self.banner.pack(fill="x", padx=18)

    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(self, anchor="nw")
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        self.tabs.add("状态")
        self.tabs.add("设置")
        self.tabs.add("聊天记录")
        self.tabs.add("日志")
        self._build_status_tab(self.tabs.tab("状态"))
        self._build_settings_tab(self.tabs.tab("设置"))
        self._build_chatlog_tab(self.tabs.tab("聊天记录"))
        self._build_log_tab(self.tabs.tab("日志"))

    # ---------- 状态页 ----------

    def _build_status_tab(self, parent):
        cards = ctk.CTkFrame(parent, fg_color="transparent")
        cards.pack(fill="x", pady=(8, 4))
        self._cards = {}
        specs = [("wechat", "微信连接"), ("watch", "监控对象"),
                 ("today", "今日已回复"), ("uptime", "运行时长")]
        for i, (key, title) in enumerate(specs):
            card = ctk.CTkFrame(cards, corner_radius=10)
            card.grid(row=0, column=i, padx=6, pady=4, sticky="nsew")
            cards.grid_columnconfigure(i, weight=1)
            ctk.CTkLabel(card, text=title, text_color=GRAY,
                         font=("微软雅黑", 12)).pack(pady=(10, 0))
            lbl = ctk.CTkLabel(card, text="—", font=("微软雅黑", 16, "bold"))
            lbl.pack(pady=(2, 12))
            self._cards[key] = lbl

        ctk.CTkLabel(parent, text="最近回复", font=("微软雅黑", 14, "bold"),
                     anchor="w").pack(fill="x", padx=8, pady=(10, 2))
        self.reply_box = ctk.CTkTextbox(parent, font=("微软雅黑", 12))
        self.reply_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.reply_box.insert("1.0", "暂无回复记录。启动监控后，自动回复会显示在这里。")
        self.reply_box.configure(state="disabled")

    # ---------- 设置页 ----------

    def _build_settings_tab(self, parent):
        scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        scroll.pack(fill="both", expand=True)

        def section(title):
            ctk.CTkLabel(scroll, text=title, font=("微软雅黑", 14, "bold"),
                         anchor="w").pack(fill="x", pady=(14, 4))

        def save_btn(parent_widget, cmd):
            return ctk.CTkButton(parent_widget, text="保存", width=90, command=cmd)

        # --- 监控对象 ---
        section("监控对象（每行一个昵称/备注名，必须与微信会话列表显示一致；仅支持私聊）")
        self.watch_box = ctk.CTkTextbox(scroll, height=80, font=("微软雅黑", 13))
        self.watch_box.pack(fill="x", padx=4)
        row = ctk.CTkFrame(scroll, fg_color="transparent")
        row.pack(fill="x", pady=4)
        self.watch_hint = ctk.CTkLabel(row, text="", text_color=GREEN, anchor="w")
        self.watch_hint.pack(side="left")
        save_btn(row, self._save_watch).pack(side="right")

        # --- AI 接口 ---
        section("AI 接口（填好后点保存；Key 保存在本机 .env，不会上传）")
        api = ctk.CTkFrame(scroll, fg_color="transparent")
        api.pack(fill="x", padx=4)
        ctk.CTkLabel(api, text="厂商", width=70, anchor="w").grid(row=0, column=0, pady=3)
        self.api_provider = ctk.CTkOptionMenu(
            api, values=list(API_PRESETS), width=170,
            command=self._on_provider_change)
        self.api_provider.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(api, text="Base URL", width=70, anchor="w").grid(row=1, column=0, pady=3)
        self.api_base = ctk.CTkEntry(api, width=380, placeholder_text="https://api.deepseek.com/v1")
        self.api_base.grid(row=1, column=1, sticky="w", pady=3)
        ctk.CTkLabel(api, text="API Key", width=70, anchor="w").grid(row=2, column=0, pady=3)
        self.api_key = ctk.CTkEntry(api, width=380, show="•", placeholder_text="sk-...")
        self.api_key.grid(row=2, column=1, sticky="w", pady=3)
        ctk.CTkLabel(api, text="模型名", width=70, anchor="w").grid(row=3, column=0, pady=3)
        self.api_model = ctk.CTkEntry(api, width=380, placeholder_text="deepseek-chat")
        self.api_model.grid(row=3, column=1, sticky="w", pady=3)
        row = ctk.CTkFrame(scroll, fg_color="transparent")
        row.pack(fill="x", pady=4)
        self.api_hint = ctk.CTkLabel(row, text="", text_color=GREEN, anchor="w")
        self.api_hint.pack(side="left")
        save_btn(row, self._save_api).pack(side="right")

        # --- 人设 ---
        section("人设中心（选预设自动填充，可自由编辑；保存后下一条回复即生效）")
        persona = ctk.CTkFrame(scroll, fg_color="transparent")
        persona.pack(fill="x", padx=4)
        ctk.CTkLabel(persona, text="预设", width=70, anchor="w").pack(side="left")
        self.preset_menu = ctk.CTkOptionMenu(
            persona, values=list(PRESETS.keys()) + ["自定义"], width=170,
            command=self._on_preset_change)
        self.preset_menu.set(self.settings.get("preset", DEFAULT_PRESET))
        self.preset_menu.pack(side="left", padx=6)
        ctk.CTkButton(persona, text="恢复此预设", width=110,
                      command=self._restore_preset).pack(side="left", padx=6)
        self.persona_box = ctk.CTkTextbox(scroll, height=200, font=("微软雅黑", 13))
        self.persona_box.pack(fill="x", padx=4, pady=4)
        row = ctk.CTkFrame(scroll, fg_color="transparent")
        row.pack(fill="x", pady=4)
        self.persona_hint = ctk.CTkLabel(row, text="", text_color=GREEN, anchor="w")
        self.persona_hint.pack(side="left")
        save_btn(row, self._save_persona).pack(side="right")

        # --- 高级 ---
        section("高级选项（一般不用动）")
        adv = ctk.CTkFrame(scroll, fg_color="transparent")
        adv.pack(fill="x", padx=4)
        ctk.CTkLabel(adv, text="检测间隔（秒，≥5）", width=140, anchor="w").grid(row=0, column=0, pady=3)
        self.adv_poll = ctk.CTkEntry(adv, width=90, placeholder_text="20")
        self.adv_poll.grid(row=0, column=1, sticky="w", pady=3)
        ctk.CTkLabel(adv, text="每小时回复上限（0=不限）", width=170, anchor="w").grid(row=0, column=2, pady=3, padx=(20, 0))
        self.adv_rate = ctk.CTkEntry(adv, width=90, placeholder_text="0")
        self.adv_rate.grid(row=0, column=3, sticky="w", pady=3)
        ctk.CTkLabel(adv, text="敏感词（逗号分隔，命中不代回）", width=200, anchor="w").grid(row=1, column=0, pady=3)
        self.adv_sensitive = ctk.CTkEntry(adv, width=420, placeholder_text="转账,红包,借钱,验证码")
        self.adv_sensitive.grid(row=1, column=1, columnspan=3, sticky="w", pady=3)
        ctk.CTkLabel(adv, text="静默时段（HH:MM-HH:MM，留空=全天）", width=220, anchor="w").grid(row=2, column=0, pady=3)
        self.adv_quiet = ctk.CTkEntry(adv, width=160, placeholder_text="02:00-07:30")
        self.adv_quiet.grid(row=2, column=1, columnspan=3, sticky="w", pady=3)
        ctk.CTkLabel(adv, text="回复模式", width=140, anchor="w").grid(row=3, column=0, pady=3)
        self.adv_mode = ctk.CTkOptionMenu(
            adv, width=220,
            values=["多人轮询模式（红点触发，回复后切走）",
                    "单聊常驻模式（停在聊天页，句句有回应）"],
            command=lambda _c: None)
        self.adv_mode.grid(row=3, column=1, columnspan=3, sticky="w", pady=3)
        row = ctk.CTkFrame(scroll, fg_color="transparent")
        row.pack(fill="x", pady=4)
        self.adv_hint = ctk.CTkLabel(row, text="", text_color=GREEN, anchor="w")
        self.adv_hint.pack(side="left")
        save_btn(row, self._save_advanced).pack(side="right")

    # ---------- 聊天记录页 ----------

    def _build_chatlog_tab(self, parent):
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", pady=(6, 4))
        ctk.CTkLabel(bar, text="联系人", font=("微软雅黑", 13)).pack(side="left")
        self.chat_contact_menu = ctk.CTkOptionMenu(
            bar, values=["（无记录）"], width=200,
            command=lambda _c: self._refresh_chat_history())
        self.chat_contact_menu.set("（无记录）")
        self.chat_contact_menu.pack(side="left", padx=8)
        ctk.CTkButton(bar, text="刷新", width=80,
                      command=self._refresh_chat_tab).pack(side="left", padx=4)

        btns = ctk.CTkFrame(parent, fg_color="transparent")
        btns.pack(fill="x", pady=4)
        ctk.CTkButton(btns, text="导出为 TXT", width=120,
                      command=self._export_chat).pack(side="left", padx=(0, 8))
        ctk.CTkButton(btns, text="清空此联系人记录", width=140,
                      fg_color=ORANGE, hover_color="#c07f1f",
                      command=self._clear_this_chat).pack(side="left", padx=8)
        ctk.CTkButton(btns, text="清空全部记录", width=120,
                      fg_color=RED, hover_color="#b8384a",
                      command=self._clear_all_chats).pack(side="left", padx=8)
        self.chat_hint = ctk.CTkLabel(parent, text="", text_color=GREEN, anchor="w")
        self.chat_hint.pack(fill="x", padx=6)

        self.chat_box = ctk.CTkTextbox(parent, font=("微软雅黑", 13))
        self.chat_box.pack(fill="both", expand=True, padx=4, pady=(4, 6))
        self.chat_box.insert("1.0", "暂无聊天记录。机器人与监控对象的对话会显示在这里（最近 24 条/人）。")
        self.chat_box.configure(state="disabled")

    def _refresh_chat_tab(self):
        contacts = self.controller.chat_contacts()
        if contacts:
            self.chat_contact_menu.configure(values=contacts)
            if self.chat_contact_menu.get() not in contacts:
                self.chat_contact_menu.set(contacts[0])
            self._refresh_chat_history()
        else:
            self.chat_contact_menu.configure(values=["（无记录）"])
            self.chat_contact_menu.set("（无记录）")
            self.chat_box.configure(state="normal")
            self.chat_box.delete("1.0", "end")
            self.chat_box.insert("1.0", "暂无聊天记录。")
            self.chat_box.configure(state="disabled")

    def _refresh_chat_history(self):
        contact = self.chat_contact_menu.get()
        if contact in ("", "（无记录）"):
            return
        text = self.controller.chat_history_text(contact)
        if self.chat_box.get("1.0", "end-1c") == text:
            return                      # 内容没变，不动滚轮位置
        self.chat_box.configure(state="normal")
        self.chat_box.delete("1.0", "end")
        self.chat_box.insert("1.0", text)
        self.chat_box.see("end")
        self.chat_box.configure(state="disabled")

    def _export_chat(self):
        import tkinter.filedialog as fd
        contact = self.chat_contact_menu.get()
        if contact in ("", "（无记录）"):
            self.chat_hint.configure(text="没有可导出的内容", text_color=RED)
            return
        target = "" if contact == "全部" else contact
        default_name = f"聊天记录_{contact or '全部'}_{datetime.now():%Y%m%d_%H%M}.txt"
        filepath = fd.asksaveasfilename(
            defaultextension=".txt", initialfile=default_name,
            filetypes=[("文本文件", "*.txt")])
        if not filepath:
            return
        ok, msg = self.controller.export_chat(filepath, target or None)
        self.chat_hint.configure(text=msg, text_color=GREEN if ok else RED)

    def _clear_this_chat(self):
        import tkinter.messagebox as mb
        contact = self.chat_contact_menu.get()
        if contact in ("", "（无记录）"):
            return
        if not mb.askyesno("确认", f"确定清空 {contact} 的聊天记录？\n清空后机器人将不再记得之前的对话内容。"):
            return
        ok, msg = self.controller.clear_chat(contact)
        self.chat_hint.configure(text=msg, text_color=GREEN if ok else RED)
        self._refresh_chat_tab()

    def _clear_all_chats(self):
        import tkinter.messagebox as mb
        if not mb.askyesno("确认", "确定清空全部聊天记录？\n清空后机器人将不再记得任何对话内容。"):
            return
        ok, msg = self.controller.clear_all_chats()
        self.chat_hint.configure(text=msg, text_color=GREEN if ok else RED)
        self._refresh_chat_tab()

    # ---------- 日志页 ----------

    def _build_log_tab(self, parent):
        bar = ctk.CTkFrame(parent, fg_color="transparent")
        bar.pack(fill="x", pady=(6, 4))
        ctk.CTkLabel(bar, text="运行日志（最近 200 行，实时刷新）",
                     font=("微软雅黑", 13), anchor="w").pack(side="left")
        ctk.CTkButton(bar, text="打开日志文件夹", width=130,
                      command=self._open_log_dir).pack(side="right")
        self.log_box = ctk.CTkTextbox(parent, font=("Consolas", 12))
        self.log_box.pack(fill="both", expand=True, padx=4, pady=(0, 6))
        self.log_box.configure(state="disabled")

    # ================= 设置装载 =================

    def _load_settings_to_ui(self):
        s = self.settings
        self.watch_box.insert("1.0", "\n".join(s.get("watch_list", [])))
        if s.get("base_url"):
            self.api_base.insert(0, s["base_url"])
        if s.get("api_key"):
            self.api_key.insert(0, s["api_key"])
        if s.get("model"):
            self.api_model.insert(0, s["model"])
        self.persona_box.insert("1.0", s.get("persona_text", ""))
        self.adv_poll.insert(0, str(s.get("ocr_poll", 20)))
        self.adv_rate.insert(0, str(s.get("max_replies_per_hour", 0)))
        self.adv_sensitive.insert(0, "，".join(s.get("sensitive_keywords", [])))
        qh = s.get("quiet_hours", [])
        if len(qh) == 2:
            self.adv_quiet.insert(0, f"{qh[0]}-{qh[1]}")
        mode_label = {"single": "单聊常驻模式（停在聊天页，句句有回应）",
                      "multi": "多人轮询模式（红点触发，回复后切走）"}
        self.adv_mode.set(mode_label.get(s.get("reply_mode", "multi"),
                                         mode_label["multi"]))
        self._refresh_banner()

    # ================= 事件处理 =================

    def _on_provider_change(self, name):
        base, model = API_PRESETS.get(name, ("", ""))
        if base:
            self.api_base.delete(0, "end")
            self.api_base.insert(0, base)
        if model:
            self.api_model.delete(0, "end")
            self.api_model.insert(0, model)

    def _on_preset_change(self, name):
        if name != "自定义" and name in PRESETS:
            self.persona_box.delete("1.0", "end")
            self.persona_box.insert("1.0", PRESETS[name])

    def _restore_preset(self):
        name = self.preset_menu.get()
        if name != "自定义" and name in PRESETS:
            self.persona_box.delete("1.0", "end")
            self.persona_box.insert("1.0", PRESETS[name])
            self.persona_hint.configure(text=f"已载入预设「{name}」，记得点保存")

    def _save_watch(self):
        names = [n.strip() for n in self.watch_box.get("1.0", "end-1c").splitlines() if n.strip()]
        ok, msg = self.controller.save_watch_list(names)
        self.watch_hint.configure(text=msg if ok else msg, text_color=GREEN if ok else RED)
        self._refresh_banner()

    def _save_api(self):
        ok, msg = self.controller.save_api(self.api_base.get(), self.api_key.get(),
                                           self.api_model.get())
        self.api_hint.configure(text=msg, text_color=GREEN if ok else RED)
        self._refresh_banner()

    def _save_persona(self):
        ok, msg = self.controller.save_persona(self.persona_box.get("1.0", "end-1c"))
        self.persona_hint.configure(text=msg, text_color=GREEN if ok else RED)

    def _save_advanced(self):
        try:
            poll = max(5, int(self.adv_poll.get() or 20))
            rate = int(self.adv_rate.get() or 0)
        except ValueError:
            self.adv_hint.configure(text="检测间隔/上限必须是数字", text_color=RED)
            return
        sensitive = [w.strip() for w in
                     self.adv_sensitive.get().replace("，", ",").split(",") if w.strip()]
        quiet_raw = self.adv_quiet.get().strip()
        quiet = [p.strip() for p in quiet_raw.split("-")] if "-" in quiet_raw else []
        if len(quiet) != 2:
            quiet = []
        mode = "single" if "单聊" in self.adv_mode.get() else "multi"
        ok, msg = self.controller.save_advanced(poll, sensitive, quiet, rate, mode)
        self.adv_hint.configure(text=msg, text_color=GREEN if ok else RED)

    def _on_start_stop(self):
        if self.controller.monitoring:
            ok, msg = self.controller.stop_monitoring()
        else:
            ok, msg = self.controller.start_monitoring()
        if not ok:
            self.banner.configure(text=msg, text_color=RED)
        self._force = True                 # 立即刷新一次（_refresh_loop 会消费）

    def _on_pause(self):
        ok, msg = self.controller.toggle_pause()
        if not ok:
            self.banner.configure(text=msg, text_color=RED)
        self._force = True

    def _on_close(self):
        """关闭窗口 = 缩到托盘继续监控；托盘不可用则最小化到任务栏"""
        if self._tray_ok:
            self.withdraw()
        else:
            self.iconify()

    def _really_exit(self):
        if self.controller.monitoring:
            self.controller.stop_monitoring()
        self.tray.stop()
        self.destroy()

    def _open_log_dir(self):
        from .config import LOG_DIR
        LOG_DIR.mkdir(exist_ok=True)
        os.startfile(LOG_DIR)

    # ================= 周期刷新 =================

    def _drain_actions(self):
        """托盘菜单回调 → 主线程执行"""
        try:
            while True:
                action, _ = self._actions.get_nowait()
                if action == "show":
                    self.after(0, self._show_from_tray)
                elif action == "pause":
                    self.after(0, self._on_pause)
                elif action == "exit":
                    self.after(0, self._really_exit)
        except queue.Empty:
            pass
        self.after(300, self._drain_actions)

    def _show_from_tray(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _refresh_loop(self):
        self._tick += 1
        try:
            snap = self.controller.status.snapshot()
            self._apply_snapshot(snap)
        except Exception as e:
            logging.debug("界面刷新异常: %s", e)
        force = getattr(self, "_force", False)
        self._force = False
        if (self.tabs.get() == "日志" and self._tick % 2 == 0) or force:
            self._refresh_log()
        if (self.tabs.get() == "聊天记录" and self._tick % 3 == 0) or force:
            self._refresh_chat_tab()
        self.after(1000, self._refresh_loop)

    def _apply_snapshot(self, snap):
        # 微信状态点
        if snap["wechat_ok"]:
            self.wx_dot.configure(text="● 微信已连接", text_color=GREEN)
        else:
            self.wx_dot.configure(text="● 未检测到微信（请登录 PC 微信）", text_color=RED)
        # 卡片
        self._cards["wechat"].configure(
            text="在线" if snap["wechat_ok"] else "离线",
            text_color=GREEN if snap["wechat_ok"] else RED)
        self._cards["watch"].configure(text=str(len(snap["watch_list"])) + " 人")
        self._cards["today"].configure(text=str(snap["replies_today"]))
        self._cards["uptime"].configure(
            text=f"{snap['runtime_min']} 分钟" if snap["running"] else "未运行")
        # 主按钮 / 暂停按钮
        if snap["running"]:
            self.start_btn.configure(text="停止监控", fg_color=RED, hover_color="#b8384a")
            self.pause_btn.configure(state="normal",
                                     text="恢复" if snap["paused"] else "暂停")
        else:
            self.start_btn.configure(text="启动监控", fg_color=None,
                                     hover_color="#1f6fd0")
            self.pause_btn.configure(state="disabled", text="暂停")
        # 横幅提示
        self._refresh_banner(snap)
        # 最近回复
        rec = snap["recent_replies"]
        if rec:
            lines = [f"{datetime.fromtimestamp(ts):%m-%d %H:%M}  → {contact}: {text}"
                     for ts, contact, text in reversed(rec)]
            current = self.reply_box.get("1.0", "end-1c")
            new_text = "\n".join(lines)
            if current != new_text:
                self.reply_box.configure(state="normal")
                self.reply_box.delete("1.0", "end")
                self.reply_box.insert("1.0", new_text)
                self.reply_box.configure(state="disabled")
        # 托盘提示
        if self._tray_ok:
            tip = "微信自动回复助手（已暂停）" if snap["paused"] else \
                  ("微信自动回复助手（监控中）" if snap["running"] else "微信自动回复助手（未启动）")
            self.tray.set_tooltip(tip)

    def _refresh_banner(self, snap=None):
        """顶部横幅：引导用户完成必要配置"""
        settings, err = load_llm_settings()
        if settings is None:
            self.banner.configure(
                text=f"⚠ 请先在【设置 → AI 接口】完成配置（{err}），然后点「启动监控」",
                text_color=ORANGE)
            return
        if snap and not snap["running"] and not snap["last_error"]:
            self.banner.configure(text="已就绪。点右上角「启动监控」开始工作。",
                                  text_color=GRAY)
            return
        if snap and snap["paused"]:
            self.banner.configure(text="监控已暂停，点「恢复」继续。", text_color=ORANGE)
            return
        if snap and snap["last_error"]:
            self.banner.configure(text=snap["last_error"], text_color=RED)
            return
        self.banner.configure(text="")

    def _refresh_log(self):
        logfile = LOG_DIR / f"bot_{datetime.now():%Y%m%d}.log"
        if not logfile.exists():
            return
        lines = []
        with suppress_io_error():
            with open(logfile, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-200:]
        text = "".join(lines).rstrip()
        if text == self._log_cache:
            return
        self._log_cache = text
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("1.0", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")


def suppress_io_error():
    from contextlib import suppress
    return suppress(OSError)


def run_gui():
    setup_logging()
    app = AssistantApp()
    app.mainloop()


def main():
    run_gui()


if __name__ == "__main__":
    main()
