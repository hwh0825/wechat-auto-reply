# -*- coding: utf-8 -*-
"""控制台入口：源码调试用。

  python main.py             无界面直接运行监控（日志打到屏幕+文件），Ctrl+C 退出
  python main.py --check     体检：微信连接/监控对象/当前状态，不发消息
  python main.py --test-send 往文件传输助手发条测试消息，验证发送链路

正式使用请运行 app.py（图形界面）或打包后的 exe（见 build_app.py）。
"""
import logging
import signal
import sys
import threading
import time

from wechat_bot import keys
from wechat_bot.config import load_config, load_llm_settings, load_persona
from wechat_bot.guard import Guard
from wechat_bot.llm import build_system_prompt
from wechat_bot.monitor import run_forever
from wechat_bot.runtime import (RuntimeStatus, acquire_single_instance,
                                release_single_instance, setup_logging)
from wechat_bot.state import State


def check_mode(cfg):
    """体检：不发消息，逐项检查前置条件"""
    from wechat_bot import watcher
    import win32gui

    ok = True
    hwnd = keys.find_wechat_window()
    logging.info("[1/3] 微信主窗口: %s", "找到" if hwnd else "未找到（请登录微信）")
    if not hwnd:
        return False

    logging.info("[2/3] 窗口可见性: %s",
                 "可见" if win32gui.IsWindowVisible(hwnd) else "托盘/最小化（检测会隐形截图）")
    img = watcher.capture_wechat()
    if img is None:
        logging.error("[2/3] 隐形截图失败")
        return False
    img.save("debug_check.png")
    logging.info("[2/3] 隐形截图成功，已存 debug_check.png")

    rows = watcher.read_watch_rows(img, cfg["watch_list"])
    logging.info("[3/3] 监控名单解析:")
    for contact in cfg["watch_list"]:
        row = rows.get(contact)
        if row:
            logging.info("    %s | 预览=%r | 未读=%s", contact, row["preview"][:30], row["badge"])
        else:
            logging.warning("    %s 不在当前可见会话列表中", contact)
            ok = False
    logging.info("体检%s", "通过 ✓" if ok else "存在未通过项 ✗")
    return ok


def main():
    logfile = setup_logging()
    logging.info("微信自动回复助手（控制台模式）启动，日志: %s", logfile)

    llm_settings, llm_err = load_llm_settings(fatal=True)
    cfg = load_config()
    system_prompt = build_system_prompt(load_persona())

    if "--check" in sys.argv:
        check_mode(cfg)
        return
    if "--test-send" in sys.argv:
        ok = keys.test_send()
        logging.info("发送链路测试: %s（去微信的文件传输助手看一眼）",
                     "成功" if ok else "失败，请人工确认")
        return

    if not acquire_single_instance():
        sys.exit("[启动失败] 检测到另一个助手实例正在运行，请勿双开。")

    status = RuntimeStatus()
    status.set_running(True, started_at=time.time())
    stop_event = threading.Event()
    try:
        signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    except Exception:
        pass

    try:
        run_forever(cfg, Guard(cfg), State(), system_prompt, "", llm_settings,
                    status=status, stop_event=stop_event)
    finally:
        release_single_instance()
        status.set_running(False)
        logging.info("助手已退出")


if __name__ == "__main__":
    main()
