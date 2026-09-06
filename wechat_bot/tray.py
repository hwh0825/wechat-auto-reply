# -*- coding: utf-8 -*-
"""系统托盘：最小化驻留、右键菜单（显示主界面/暂停/恢复/退出）。

pystray 在独立线程运行；菜单回调通过 action_queue 交还 GUI 主线程执行，
避免跨线程操作 tkinter。
"""
import logging
import queue
from contextlib import suppress

import pystray
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
                   "C:/Windows/Fonts/simhei.ttf"]


def _draw_icon(size: int = 64) -> Image.Image:
    """画一个橙底猫耳"喵"图标"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    # 圆角底 + 猫耳
    d.rounded_rectangle([6 * s, 14 * s, 58 * s, 58 * s], radius=14 * s,
                        fill=(245, 166, 35, 255))
    d.polygon([(12 * s, 26 * s), (22 * s, 6 * s), (30 * s, 22 * s)],
              fill=(245, 166, 35, 255))
    d.polygon([(52 * s, 26 * s), (42 * s, 6 * s), (34 * s, 22 * s)],
              fill=(245, 166, 35, 255))
    # "喵"字
    font = None
    for path in FONT_CANDIDATES:
        with suppress(Exception):
            font = ImageFont.truetype(path, int(26 * s))
            break
    d.text((32 * s, 37 * s), "喵", font=font, fill=(255, 255, 255, 255), anchor="mm")
    return img


class TrayIcon:
    """托盘图标。回调在 pystray 线程触发，动作对象放入 action_queue 由 GUI 轮询执行。"""

    def __init__(self, action_queue: queue.Queue):
        self.q = action_queue
        self.icon = pystray.Icon(
            "微信自动回复助手",
            icon=_draw_icon(),
            title="微信自动回复助手",
            menu=pystray.Menu(
                pystray.MenuItem("显示主界面", lambda: self.q.put(("show", None)),
                                 default=True),
                pystray.MenuItem("暂停/恢复监控", lambda: self.q.put(("pause", None))),
                pystray.MenuItem("退出", lambda: self.q.put(("exit", None))),
            ))

    def start(self):
        """在后台线程启动托盘。失败（极端环境）返回 False，界面退化为普通窗口。"""
        with suppress(Exception):
            self.icon.run_detached()
            self.icon.visible = True
            return True
        logging.warning("系统托盘不可用，关闭窗口将直接最小化到任务栏")
        return False

    def set_tooltip(self, text: str):
        with suppress(Exception):
            self.icon.title = text

    def stop(self):
        with suppress(Exception):
            self.icon.stop()
