# -*- coding: utf-8 -*-
"""键盘+OCR 自动化回复：激活微信 → 搜索联系人 → 点击结果 → 校验标题 → 粘贴发送

微信 4.1.13 界面自绘、无控件树，搜索结果回车会触发"搜一搜"而非进入会话，
所以用 OCR 定位搜索结果行并真实点击。所有中文走剪贴板粘贴，避开输入法。
"""
import ctypes
import logging
import time
from contextlib import suppress

import pyperclip
import win32gui
from PIL import ImageGrab

WECHAT_WINDOW_CLASS = "Qt51514QWindowIcon"
WECHAT_WINDOW_TITLE = "微信"

VK_CTRL, VK_MENU, VK_RETURN, VK_ESCAPE, VK_BACK = 0x11, 0x12, 0x0D, 0x1B, 0x08
VK_V = 0x56

_user32 = ctypes.windll.user32
_ocr = None

# 统一坐标系：进程声明 DPI 感知后，截图、窗口矩形、鼠标坐标全是物理像素
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE
except Exception:
    with suppress(Exception):
        _user32.SetProcessDPIAware()


def _key(vk, down: bool):
    _user32.keybd_event(vk, 0, 0 if down else 2, 0)


def _combo(*vks, hold=0.05):
    for vk in vks:
        _key(vk, True)
        time.sleep(hold)
    for vk in reversed(vks):
        _key(vk, False)
        time.sleep(hold)


def find_wechat_window():
    hwnd = win32gui.FindWindow(WECHAT_WINDOW_CLASS, WECHAT_WINDOW_TITLE)
    if hwnd:
        return hwnd
    result = []

    def _cb(h, _):
        if WECHAT_WINDOW_TITLE == (win32gui.GetWindowText(h) or "").strip():
            result.append(h)

    win32gui.EnumWindows(_cb, None)
    return result[0] if result else 0


def focus_window(hwnd) -> bool:
    """强制前置，且必须确认前台真的是微信（否则后续按键会落进别的应用）"""
    _user32.keybd_event(VK_MENU, 0, 0, 0)      # Alt down（解锁 SetForegroundWindow）
    try:
        win32gui.ShowWindow(hwnd, 9)            # SW_RESTORE
        _ = bool(_user32.SetForegroundWindow(hwnd))
    finally:
        _user32.keybd_event(VK_MENU, 0, 2, 0)   # Alt up
    time.sleep(0.4)
    if _user32.GetForegroundWindow() != hwnd:
        logging.warning("微信窗口前置失败，取消本次操作")
        return False
    return True


# ---------------- OCR ----------------

def _get_ocr():
    global _ocr
    if _ocr is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr = RapidOCR()
    return _ocr


def ocr_image_lines(img):
    """OCR 一张图片，返回 [(text, 中心x, 中心y)]（物理像素坐标）"""
    result, _ = _get_ocr()(img)
    lines = []
    for box, text, score in (result or []):
        if score and float(score) > 0.5 and text.strip():
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            lines.append((text.strip(), sum(xs) / len(xs), sum(ys) / len(ys)))
    return lines


def ocr_image_boxes(img):
    """同 ocr_image_lines，但额外保留文字框坐标：
    返回 [(text, 中心x, 中心y, (x0, y0, x1, y1))]，供气泡底色判断使用。"""
    result, _ = _get_ocr()(img)
    lines = []
    for box, text, score in (result or []):
        if score and float(score) > 0.5 and text.strip():
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            lines.append((text.strip(), sum(xs) / len(xs), sum(ys) / len(ys),
                          (min(xs), min(ys), max(xs), max(ys))))
    return lines


def _capture_lines():
    """新鲜截图微信窗口并 OCR（前台走屏幕截图，避免 GPU 窗口过时帧）。

    返回 (lines, wl, wt)（lines 为窗口局部坐标），失败 None。
    """
    from . import watcher          # 延迟导入，避免 keys↔watcher 循环依赖
    hwnd = find_wechat_window()
    if not hwnd:
        return None
    img = watcher.capture_fresh()
    if img is None:
        return None
    wl, wt, _, _ = win32gui.GetWindowRect(hwnd)
    return ocr_image_lines(img), wl, wt


def _click(x, y):
    _user32.SetCursorPos(int(x), int(y))
    time.sleep(0.1)
    _user32.mouse_event(2, 0, 0, 0, 0)   # LEFTDOWN
    time.sleep(0.06)
    _user32.mouse_event(4, 0, 0, 0, 0)   # LEFTUP


def _right_click(x, y):
    _user32.SetCursorPos(int(x), int(y))
    time.sleep(0.1)
    _user32.mouse_event(8, 0, 0, 0, 0)   # RIGHTDOWN
    time.sleep(0.06)
    _user32.mouse_event(16, 0, 0, 0, 0)  # RIGHTUP



def _foreground_ok(hwnd) -> bool:
    """鼠标落点校验：点击目标必须真在微信窗口上（点别处会把菜单/输入
    打进用户正在用的程序）。"""
    return bool(hwnd) and _user32.GetForegroundWindow() == hwnd



def _paste_clipboard(hwnd, text: str, delay: float) -> bool:
    """把 text 粘贴到当前焦点控件（备份版本实测可用的序列，勿改动节奏）：

    写剪贴板 → 0.15s → Ctrl+V → 等 delay(1.2s) → 恢复原剪贴板。
    微信读取剪贴板是异步的，等待必须给足 1 秒以上；之前压到 0.4s 后
    粘贴就再也进不了输入框。加 Ctrl+A 也会破坏粘贴，同样不要加。
    前台校验只用于放弃时机：用户正在用电脑时宁可本轮不发，
    也不能把内容打进别的程序。
    """
    if not _foreground_ok(hwnd):
        logging.warning("粘贴时微信不在前台，本轮取消")
        return False
    prev = None
    with suppress(Exception):
        prev = pyperclip.paste()
    pyperclip.copy(text)
    time.sleep(0.15)
    _combo(VK_CTRL, VK_V, hold=0.08)
    time.sleep(delay)
    with suppress(Exception):
        if prev:
            pyperclip.copy(prev)
    return True


# ---------------- 发送流程 ----------------



def _click_search_result(hwnd, contact: str) -> bool:
    """在搜索结果面板里找到并点击联系人那一行（左侧列表区域里最靠上的完全匹配）。

    搜索结果面板是独立弹窗，PrintWindow 抓不到主窗口位图，必须截全屏；
    因此要求微信在前台（调用方已 focus），点击前再校验一次，防止点进别的程序。
    """
    if not _foreground_ok(hwnd):
        logging.warning("定位搜索结果时微信不在前台，取消点击")
        return False
    lines = ocr_image_lines(ImageGrab.grab())
    wl, wt, wr, hb = win32gui.GetWindowRect(hwnd)
    win_w = wr - wl
    win_h = hb - wt
    candidates = []
    for t, x, y in lines:
        # 必须在左侧面板、且在搜索输入框下方——输入框里的联系人文字不是结果
        if (t == contact and wl <= x <= wl + win_w * 0.25
                and y > wt + 110 and y < wt + win_h):
            candidates.append((t, x, y))
    if not candidates:
        logging.warning("搜索结果中未找到完全匹配项 %s", contact)
        return False
    candidates.sort(key=lambda c: c[2])
    _, x, y = candidates[0]
    _click(x, y)
    time.sleep(1.2)
    return True


def _chat_title_ok(hwnd, contact: str) -> bool:
    """校验聊天窗口标题区确实是目标联系人（基于隐形截图，不依赖前台）。

    只认右侧聊天窗格的标题（实测标题紧跟会话列表之后，约在窗口左缘 +480px，
    搜索框残留文本在 +220px 以内），固定 300px 阈值即可区分；
    不能按窗口宽度比例取阈值——宽窗口上比例值会越过标题本身，
    导致"明明在目标会话却校验失败"的死循环。
    """
    cap = _capture_lines()
    if cap is None:
        logging.warning("聊天标题校验失败：微信窗口不可用")
        return False
    lines, _wl, _wt = cap
    for t, x, y in lines:
        if t.strip() == contact.strip() and y <= 95 and x >= 300:
            return True
    logging.warning("聊天标题校验失败：目标 %s 不在标题区", contact)
    return False


def _click_search_box(hwnd) -> bool:
    """点击微信左上角的搜索框（OCR 定位），保证焦点落在搜索框里"""
    cap = _capture_lines()
    if cap is None:
        logging.warning("微信窗口不可用，无法定位搜索框")
        return False
    lines, wl, wt = cap
    for t, x, y in lines:
        # 搜索框在窗口左上角，占位文案是"搜索"（OCR 可能带上图标前缀）
        if ("搜索" in t and len(t) <= 5
                and x <= 320 and y <= 110):
            _click(wl + x, wt + y)
            time.sleep(0.6)
            return True
    logging.warning("未定位到左上角搜索框")
    return False


def _click_chat_input(hwnd):
    """点击聊天输入框区域，确保粘贴目标是对话输入框（而不是搜一搜等面板）。

    注意：这里不能按 Esc、不能挪点击点——备份版本（实测可用）就是
    直接点距底 85px。加 Esc 后微信输入框会收不到粘贴，原因未明但可复现。
    """
    rect = win32gui.GetWindowRect(hwnd)
    wl, wt, wr, hb = rect
    x = wl + int((wr - wl) * 0.45)   # 聊天窗格中部
    y = hb - 85                      # 底部输入区
    _click(x, y)
    time.sleep(0.3)


def open_chat(contact: str, delay: float = 1.2, verify: bool = True) -> bool:
    """前置微信并打开指定联系人聊天页，不发送任何消息。"""
    hwnd = find_wechat_window()
    if not hwnd:
        logging.error("找不到微信主窗口，无法打开聊天")
        return False
    hwnd = _focus_wechat(hwnd)
    if not hwnd:
        logging.warning("微信窗口前置失败")
        return False
    if verify and _chat_title_ok(hwnd, contact):
        return True
    if not _click_search_box(hwnd):
        return False
    if not _paste_clipboard(hwnd, contact, delay):
        _combo(VK_ESCAPE, hold=0.05)
        return False
    time.sleep(1.6)
    if not _click_search_result(hwnd, contact):
        _combo(VK_ESCAPE, hold=0.05)
        return False
    if verify and not _chat_title_ok(hwnd, contact):
        logging.error("(%s) 打开的聊天不是目标，放弃切换", contact)
        _combo(VK_ESCAPE, hold=0.05)
        return False
    logging.info("已切换到目标聊天：%s", contact)
    return True


def park_chat(delay: float = 1.2):
    """回复完成后把当前会话切换到文件传输助手。

    微信会对"当前打开的会话"自动标记已读——若停留在对方的聊天界面，
    下一条消息就没有未读红点，检测会失效。切走后红点机制恢复正常。
    结束时把窗口恢复到进入前的状态：原本藏托盘的收回托盘、还原之前的前台，
    与 README 所述"发完自动收回"保持一致，避免把微信留在前台。
    """
    hwnd = find_wechat_window()
    if not hwnd:
        return False
    prev = _user32.GetForegroundWindow()
    was_visible = bool(_user32.IsWindowVisible(hwnd))
    try:
        for attempt in range(2):
            with suppress(Exception):
                _combo(VK_ESCAPE, hold=0.05)
                focus_window(hwnd)                      # 确保微信在前台且完整可见
                if _click_search_box(hwnd):
                    if _paste_clipboard(hwnd, "文件传输助手", delay):
                        time.sleep(1.4)
                        if _click_search_result(hwnd, "文件传输助手"):
                            logging.info("已切离对方会话（停在文件传输助手）")
                            return True
                # 兜底：直接点会话列表里可见的"文件传输助手"行
                target = [(t, x, y) for t, x, y in
                          ocr_image_lines(ImageGrab.grab())
                          if t == "文件传输助手"]
                if target:
                    _, x, y = sorted(target, key=lambda c: c[2])[0]
                    _click(x, y)
                    time.sleep(0.8)
                    logging.info("已切离对方会话（列表直点）")
                    return True
            logging.warning("切离会话第 %d 次尝试失败", attempt + 1)
            time.sleep(1.0)
        logging.warning("切离会话失败，聊天界面可能停留在 %s，下一条消息可能检测不到", "对方")
        return False
    finally:
        # 无论成功与否，恢复到窗口进入前的状态：原本藏托盘就收回，并还原之前的前台
        with suppress(Exception):
            if not was_visible and win32gui.IsWindow(hwnd):
                _user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE：收回托盘
                time.sleep(0.5)
        if prev and prev != hwnd:
            with suppress(Exception):
                _user32.SetForegroundWindow(prev)


def _valid_hwnd(hwnd):
    """微信进入僵尸状态或从托盘恢复时会销毁重建窗口，旧句柄随时失效。
    每个操作阶段开始前重新拿一次有效句柄；拿不到说明微信已退出。"""
    if hwnd and win32gui.IsWindow(hwnd):
        return hwnd
    new = find_wechat_window()
    if new and new != hwnd:
        logging.info("微信窗口句柄已更新: %s -> %s", hwnd, new)
    return new


def _focus_wechat(hwnd):
    """前置微信，自动容忍句柄失效（重找一次）。返回当前有效句柄或 0。"""
    hwnd = _valid_hwnd(hwnd)
    if not hwnd:
        return 0
    if focus_window(hwnd):
        return hwnd
    hwnd = _valid_hwnd(hwnd)
    if not hwnd:
        return 0
    return hwnd if focus_window(hwnd) else 0


def send_reply(contact: str, text: str, delay: float = 1.2, verify: bool = True) -> bool:
    """完整链路：前置微信 → （需要时）搜索定位 → 校验标题 → 点输入框
    → 粘贴回复 → 回车发送。

    操作序列与实测可用的备份版本一致（点距底85、Ctrl+V 等 1.2s、回车），
    不得随意改动节奏；只保留安全措施：前台校验（用户在用电脑时放弃本轮，
    防止把内容打进别的程序）、句柄自愈（微信僵尸态/恢复时会重建窗口）、
    恢复后的渲染沉降和窗口状态还原。
    """
    hwnd = find_wechat_window()
    if not hwnd:
        logging.error("找不到微信主窗口，无法发送")
        return False
    prev = _user32.GetForegroundWindow()
    was_visible = bool(_user32.IsWindowVisible(hwnd))
    was_iconic = bool(_user32.IsIconic(hwnd))
    try:
        hwnd = _focus_wechat(hwnd)
        if not hwnd:
            logging.warning("微信窗口前置失败，取消本次发送")
            return False
        if was_iconic or not was_visible:
            time.sleep(1.5)            # 恢复后的窗口需要渲染就绪
        # 永远走完整搜索流程，不做"已在目标会话就直接粘贴"的快速路径：
        # 实测（备份对照）只有经过"点搜索结果打开会话"后，微信才会自动
        # 聚焦聊天输入框；跳过这一步时输入框处于休眠态，点击/粘贴全部无效。
        # 前台被用户抢走只放弃当前轮，立即重抢前台再试一次（共两轮），
        # 两轮都不行才交给监控循环等下个周期再试。
        last_err = "未知"
        for attempt in range(2):
            if attempt:
                _combo(VK_ESCAPE, hold=0.05)       # 关掉可能残留的搜索面板
                time.sleep(0.5)
            hwnd = _focus_wechat(hwnd) or hwnd
            if not _click_search_box(hwnd):
                last_err = "定位搜索框失败"
                continue
            _combo(VK_CTRL, 0x41, hold=0.05)       # 选中搜索框内残留文本
            _combo(VK_BACK, hold=0.05)             # 清空，避免搜索词拼接
            time.sleep(0.2)
            if not _paste_clipboard(hwnd, contact, delay):
                last_err = "搜索词粘贴失败"
                continue
            time.sleep(1.6)                        # 等搜索结果渲染
            if not _click_search_result(hwnd, contact):
                last_err = "点击搜索结果失败（前台被抢或结果未出现）"
                continue
            if verify and not _chat_title_ok(hwnd, contact):
                logging.error("(%s) 打开的聊天不是目标，放弃发送", contact)
                last_err = "标题校验失败"
                continue
            hwnd = _valid_hwnd(hwnd) or hwnd
            if not _foreground_ok(hwnd):
                focus_window(hwnd)
            if not _foreground_ok(hwnd):
                last_err = "前台被占用"
                continue
            _click_chat_input(hwnd)
            if not _paste_clipboard(hwnd, text, delay):
                last_err = "回复粘贴失败"
                continue
            _combo(VK_RETURN, hold=0.08)              # 发送
            time.sleep(0.5)
            logging.info("发送完成 (%s)", contact)
            return True
        logging.warning("(%s) 发送两轮未完成（%s），等下一轮重试", contact, last_err)
        return False
    finally:
        # 还原现场：原本最小化就最小化回去；原本藏托盘就收回；
        # 把前台还给用户之前的应用，微信不要一直霸占前台
        with suppress(Exception):
            if was_iconic and win32gui.IsWindow(hwnd) and not _user32.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, 6)       # SW_MINIMIZE
            elif not was_visible and win32gui.IsWindow(hwnd) and _user32.IsWindowVisible(hwnd):
                _user32.PostMessageW(hwnd, 0x0010, 0, 0)   # WM_CLOSE：收回托盘
            time.sleep(0.4)
        if prev and prev != hwnd:
            with suppress(Exception):
                _user32.keybd_event(VK_MENU, 0, 0, 0)      # Alt 解锁前台切换
                _user32.SetForegroundWindow(prev)
                _user32.keybd_event(VK_MENU, 0, 2, 0)


def test_send(target: str = "文件传输助手") -> bool:
    """自测：向文件传输助手发一条无害测试消息，并核对它真的出现在界面。

    文案必须和历史消息不同（此前同文案会匹配到旧消息造成假阳性），
    且核对必须用新鲜截图。
    """
    text = "【自动回复助手】链路自检消息，可忽略"
    if not send_reply(target, text, delay=1.2):
        return False
    from . import watcher          # 延迟导入，避免与 keys 的循环依赖
    time.sleep(1.5)
    for _ in range(2):
        img = watcher.capture_fresh()
        if img is not None and any("链路自检" in t for t, _x, _y in watcher.ocr_image_lines(img)):
            return True
        time.sleep(1.5)
    logging.warning("(%s) 已发送但未能在界面核对到内容，请人工确认", target)
    return False
