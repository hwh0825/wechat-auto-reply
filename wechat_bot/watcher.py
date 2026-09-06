# -*- coding: utf-8 -*-
"""OCR 轮询检测：隐形截图微信窗口 → 读会话列表 → 检测白名单联系人的未读红点

微信 4.x 私聊不发系统通知（官方机制），所以不走通知中心，直接"看"界面：
- 窗口藏在托盘时，以【屏幕外坐标 + 不抢焦点】方式临时显示，GPU 窗口即会渲染，
  用 PrintWindow(PW_RENDERFULLCONTENT) 抓帧，抓完立刻藏回，全程用户不可见。
- 窗口本来就可见时，直接抓帧，不动用户窗口。
"""
import ctypes
import time
from contextlib import suppress

import win32con
import win32gui, win32ui
from PIL import Image

from .keys import find_wechat_window, ocr_image_lines, ocr_image_boxes

user32 = ctypes.windll.user32
SWP_NOSIZE, SWP_NOACTIVATE, SWP_NOZORDER = 0x1, 0x10, 0x4
OFFSCREEN_X = -3200
PW_RENDERFULLCONTENT = 2
SW_SHOWNOACTIVATE = 4          # WINDOWPLACEMENT.showCmd：还原显示但不激活
SW_SHOWMINIMIZED = 6


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint),
                ("ptMinPosition", _POINT), ("ptMaxPosition", _POINT),
                ("rcNormalPosition", _RECT)]


def _get_placement(hwnd):
    wp = _WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(_WINDOWPLACEMENT)
    user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
    return wp


def _set_placement(hwnd, wp):
    user32.SetWindowPlacement(hwnd, ctypes.byref(wp))


def _printwindow(hwnd, w, h) -> Image.Image:
    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        raise OSError("GetWindowDC failed")
    src = mem = bmp = None
    try:
        src = win32ui.CreateDCFromHandle(hdc)
        mem = src.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(src, max(1, int(w)), max(1, int(h)))
        mem.SelectObject(bmp)
        user32.PrintWindow(hwnd, mem.GetSafeHdc(), PW_RENDERFULLCONTENT)
        info = bmp.GetInfo()
        data = bmp.GetBitmapBits(True)
        return Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]),
                               data, "raw", "BGRX", 0, 1)
    finally:
        with suppress(Exception):
            if bmp is not None:
                win32gui.DeleteObject(bmp.GetHandle())
        with suppress(Exception):
            if mem is not None:
                mem.DeleteDC()
        with suppress(Exception):
            if src is not None:
                src.DeleteDC()
        with suppress(Exception):
            user32.ReleaseDC(hwnd, hdc)


import re as _re

_TIME_LINE = _re.compile(
    r"^(?:(?:昨天|今天|前天|星期[一二三四五六日天])?\s*\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d{1,2}月\d{1,2}日(?:\s*星期[一二三四五六日天])?\s*"
    r"\d{1,2}:\d{2}(?::\d{2})?)$")


def read_chat_bubbles(img, hwnd, contact: str = ""):
    """OCR 右侧聊天窗格，解析出按时间排序的气泡列表。

    返回 (bubbles, title_ok)：
      bubbles: [(side, text)]，side 为 "them"（左侧气泡）/ "me"（右侧气泡），旧→新
      title_ok: 标题区是否出现目标联系人（contact 非空时校验）

    判定要点（均为实测）：
      - 聊天窗格起点随 DPI 变化，按窗口宽度 28% 估算
      - 输入框文字距底约 230px 起，气泡区下边界取距底 210px 挡住它
      - 我方/对方按气泡底色（绿/白）区分，见 _bubble_side
    """
    lines = ocr_image_boxes(img)

    # _printwindow() 返回的是窗口局部位图，OCR 坐标原点是 (0, 0)，
    # 不能再与 GetWindowRect() 的屏幕绝对坐标混用。
    # 微信左侧会话栏宽度随 DPI 变化（100% 约 460px、125% 约 575px），
    # 用窗口宽度比例估算起点；实测 1605px 宽窗口下列表内容 x≤455、
    # 聊天窗格内容 x≥480，28% 恰好落在两者之间。
    pane_x0 = max(400, int(img.width * 0.28))
    pane_w = img.width - pane_x0 - 20             # 聊天窗格有效宽度
    pane_x1 = img.width - 20
    # 输入区从距底约 230px 开始（语音输入提示文字实测在距底 194、
    # 工具图标行在距底 63），真实消息最低只到距底 ~270，
    # 下边界取距底 210 即可把输入框文字挡在气泡区外。
    pane_y1 = img.height - 210
    pane_y0 = 95                                  # 避开标题区
    mid = pane_x0 + pane_w * 0.50                # 颜色无法判断时的左右分界线

    title_ok = False
    rows = []
    for t, x, y, box in lines:
        t = t.strip()
        if not t:
            continue
        # 标题区（窗口顶部横条，聊天标题紧跟会话列表之后，实测 x≈500/1605px；
        # 搜索框占位文字在 x≈165，用固定 300px 即可区分，不能用 pane_x0——
        # 按窗口比例算出的起点在宽窗口上会越过标题本身，导致永远校验失败）
        if 0 <= y <= 95 and x >= 300:
            if contact and t == contact:
                title_ok = True
            continue
        if not (pane_x0 <= x <= pane_x1) or not (pane_y0 <= y <= pane_y1):
            continue
        if _TIME_LINE.match(t):
            continue
        if t in ("发送", "按住 说话") or "语音输入" in t:   # 输入区按钮/提示误识别
            continue
        rows.append((t, x, y, box))

    bubbles = []
    last_side, last_y = None, None
    for t, x, y, box in sorted(rows, key=lambda r: r[2]):
        side = _bubble_side(img, box, x, pane_x0, pane_x1, mid)
        if bubbles and side == last_side and y - last_y < 46:
            bubbles[-1] = (side, bubbles[-1][1] + " " + t)   # 同一气泡的换行合并
        else:
            bubbles.append((side, t))
        last_side, last_y = side, y
    return bubbles, title_ok


def _bubble_side(img: Image.Image, box, x: float,
                 pane_x0: int, pane_x1: int, fallback_mid: float) -> str:
    """按气泡底色判断发送方：绿底=我方，白底=对方。颜色是主判据。

    自己消息换行后的短行文字中心会落到中线左侧，按 x 坐标分类会把自己
    消息误判成对方、触发对己回复（用户实测踩坑）。文字框内部的底色
    采样不受文字长短影响。颜色不可判时（图片/表情/深色卡片气泡）
    才退回文字中心位置。
    """
    x0, y0, x1, y1 = box
    px = img.load()
    green = white = 0
    for sy in range(int(y0) + 2, int(y1) - 1, 4):
        for sx in range(int(x0) + 2, int(x1) - 1, 6):
            r, g, b = px[sx, sy][:3]
            if g > r + 18 and g > b + 18 and g > 125:
                green += 1                       # 我方气泡的微信绿
            elif r > 246 and g > 246 and b > 246:
                white += 1                       # 对方气泡白底（聊天背景是 237 灰，不会误计）
    if green >= 4 and green >= white:
        return "me"
    if white >= 4 and white > green:
        return "them"
    side_margin = max(20, int((pane_x1 - pane_x0) * 0.03))
    if x >= fallback_mid + side_margin:
        return "me"
    if x <= fallback_mid - side_margin:
        return "them"
    return "me" if x > fallback_mid else "them"


def capture_wechat():
    """隐形抓取微信窗口画面。失败返回 None。抓完把窗口恢复到原状态。

    最小化窗口通过 SetWindowPlacement 一次性"改还原位置+还原显示"：
    窗口直接在屏幕外还原，全程不出现在屏幕上（若先在原位置还原再挪走，
    窗口会闪现约 1.5 秒，用户会看到微信"一闪一闪"）。
    """
    hwnd = find_wechat_window()
    if not hwnd:
        return None
    visible = bool(user32.IsWindowVisible(hwnd))
    iconic = bool(user32.IsIconic(hwnd))
    wl, wt, _, _ = win32gui.GetWindowRect(hwnd)
    offscreen = wl < -1500          # 之前被挪到屏幕外的残留状态
    # 最小化/托盘隐藏/屏幕外的窗口不渲染，直接截图是黑屏，需要临时还原
    need_trick = (not visible) or iconic or offscreen
    saved_wp = None
    if need_trick:
        if iconic:
            saved_wp = _get_placement(hwnd)            # 原还原位置 + 最小化状态
            wp = _WINDOWPLACEMENT.from_buffer_copy(saved_wp)
            n = saved_wp.rcNormalPosition
            wp.rcNormalPosition = _RECT(OFFSCREEN_X, 100,
                                        OFFSCREEN_X + (n.right - n.left),
                                        100 + (n.bottom - n.top))
            wp.showCmd = SW_SHOWNOACTIVATE             # 直接在屏幕外还原
            _set_placement(hwnd, wp)
        else:
            wl, wt, wr, hb = win32gui.GetWindowRect(hwnd)   # 隐藏窗口也能取到真实尺寸
            w, h = max(wr - wl, 200), max(hb - wt, 200)
            user32.SetWindowPos(hwnd, 0, OFFSCREEN_X, 100, w, h,
                                SWP_NOACTIVATE | SWP_NOZORDER)
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNA)
        time.sleep(0.9)
    wl, wt, wr, hb = win32gui.GetWindowRect(hwnd)   # 显示后再取真实尺寸
    w, h = max(wr - wl, 200), max(hb - wt, 200)
    try:
        return _printwindow(hwnd, w, h)
    finally:
        if need_trick:
            with suppress(Exception):
                if iconic and saved_wp is not None:
                    _set_placement(hwnd, saved_wp)   # 原位置 + 重新最小化
                elif not visible or offscreen:
                    # 关键：隐藏前把窗口挪回屏幕内的默认位置，
                    # 否则窗口记住屏幕外坐标，下次从托盘打开会"点不出来"
                    sw = user32.GetSystemMetrics(0)
                    sh = user32.GetSystemMetrics(1)
                    user32.SetWindowPos(hwnd, 0, max(60, (sw - w) // 2),
                                        max(80, (sh - h) // 3), w, h,
                                        win32con.SWP_NOACTIVATE | win32con.SWP_NOZORDER)
                    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)  # 藏回托盘


def capture_fresh():
    """保证内容新鲜的窗口截图：前台可见时直接截屏幕——
    PrintWindow 对 GPU 渲染的窗口可能返回过时帧（连刚输入的文字都看不到）；
    其余状态（托盘/最小化/后台）走隐形抓帧。返回窗口局部坐标位图。"""
    hwnd = find_wechat_window()
    if not hwnd:
        return None
    with suppress(Exception):
        if user32.GetForegroundWindow() == hwnd and user32.IsWindowVisible(hwnd):
            from PIL import ImageGrab
            wl, wt, wr, hb = win32gui.GetWindowRect(hwnd)
            shot = ImageGrab.grab()
            return shot.crop((max(0, wl), max(0, wt),
                              min(shot.width, wr), min(shot.height, hb)))
    return capture_wechat()


def _red_badge_band(img, x_from, x_to, y_from, y_to) -> bool:
    """在矩形区域内找微信红点角标的高饱和红色像素（成簇出现才算）"""
    px = img.load()
    hits = 0
    for y in range(int(y_from), int(y_to), 2):
        for x in range(int(x_from), int(x_to), 2):
            if 0 <= x < img.width and 0 <= y < img.height:
                r, g, b = px[x, y][:3]
                if r > 170 and g < 95 and b < 95:
                    hits += 1
    return hits >= 6


def read_watch_rows(img: Image.Image, watch_list):
    """从截图解析会话列表，返回 {联系人: {"preview": str, "badge": bool, "time": str}}

    会话列表列宽是固定像素（不随窗口宽度缩放），红点在头像右上角、
    名字文本左侧，所以用名字位置反推红点区域，不能用窗口宽度等比坐标。
    """
    W = img.width
    lines = ocr_image_lines(img)

    x_lo, x_hi = W * 0.06, W * 0.44          # 会话列表横向范围
    list_lines = [(t, x, y) for t, x, y in lines if x_lo <= x <= x_hi]

    result = {}
    for contact in watch_list:
        name = [(t, x, y) for t, x, y in list_lines if t == contact]
        if not name:
            continue
        _, nx, ny = sorted(name, key=lambda c: c[2])[0]
        # 预览文本：名字行下方最近的列表文本
        below = [(t, x, y) for t, x, y in list_lines
                 if ny + 8 < y < ny + 52 and abs(x - nx) < W * 0.18]
        preview = min(below, key=lambda c: c[2])[0] if below else ""
        # 时间戳：名字行右侧
        times = [(t, x, y) for t, x, y in list_lines
                 if abs(y - ny) < 12 and x > x_hi * 0.75]
        tstr = min(times, key=lambda c: abs(c[2] - ny))[0] if times else ""
        # 红点角标：头像右上角，位于名字左侧的整片区域里扫描
        badge = _red_badge_band(img, max(40, nx - 220), nx - 15, ny - 34, ny + 8)
        result[contact] = {"preview": preview, "badge": badge, "time": tstr}
    return result
