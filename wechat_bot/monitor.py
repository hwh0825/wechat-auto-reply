# -*- coding: utf-8 -*-
"""核心监控循环：隐形截图 → 检测新消息 → 护栏 → LLM 回复 → 键盘发送

两种回复模式（config.yaml 的 reply_mode）：
  multi  多人轮询（默认）：未读红点 + 会话预览触发，回复后切到文件传输助手
  single 单聊常驻：窗口停在对方聊天页，OCR 聊天气泡逐条检测，回复后原地不动
"""
import logging
import time
from difflib import SequenceMatcher

from . import keys, llm, watcher
from .config import load_config, load_persona
from .llm import build_system_prompt
from .notify import notify

# 会话预览/气泡里没有可读内容时的占位文案
PLACEHOLDER_CONTENTS = ("你收到一条新消息", "[图片]", "[视频]", "[语音]",
                        "[文件]", "[链接]", "[卡片]", "[表情]", "[动画表情]",
                        "[转账]", "[红包]", "[拍一拍]")
# 单聊模式下这些占位涉及资金/风险，不交给 AI，转人工
RISK_PLACEHOLDERS = ("[转账]", "[红包]", "[收款码]")


def _no_detail(text: str) -> bool:
    return any(p in text for p in PLACEHOLDER_CONTENTS)


def _is_risk_placeholder(text: str) -> bool:
    return any(p in text for p in RISK_PLACEHOLDERS)


# ================= 单聊模式：气泡 diff =================

def _diff_bubbles(seen: list, bubbles: list):
    """对比已见气泡与当前气泡，返回 (新增气泡, 当前全部气泡)。

    前缀匹配：seen 恰好是 bubbles 的前缀时，新增 = 多出来的尾部；
    OCR 噪声导致前缀对不上时，回退为"最后一条已见文本之后的内容"，
    仍对不上则保守地当作没有新消息。
    """
    if not seen:
        return [], bubbles                    # 首轮全部作为基线

    # 当前截图只包含聊天窗口中可视的若干条气泡，滚动后可能在历史基线
    # 前面出现更早的气泡。因此不能要求基线是当前列表前缀，要在当前
    # 可见序列中寻找历史尾部的连续片段，再取片段后面的新增气泡。
    seen_norm = [_norm_bubble_text(t) for t in seen]
    current_norm = [_norm_bubble_text(t) for _, t in bubbles]
    max_overlap = min(len(seen_norm), len(current_norm))
    min_overlap = 1 if len(seen_norm) == 1 else 2
    for overlap in range(max_overlap, min_overlap - 1, -1):
        expected = seen_norm[-overlap:]
        for start in range(0, len(current_norm) - overlap + 1):
            actual = current_norm[start:start + overlap]
            if all(_bubble_text_matches(left, right)
                   for left, right in zip(expected, actual)):
                return bubbles[start + overlap:], bubbles
    # OCR 变化导致无法安全对齐时保守等待，避免误把旧消息当新消息。
    return [], bubbles


def _norm_bubble_text(text: str) -> str:
    """仅用于 diff 对齐的轻量 OCR 归一化，不改变实际发送内容。"""
    return " ".join(str(text).split())


def _bubble_text_matches(left: str, right: str) -> bool:
    """容忍 OCR 的少量错字/标点差异，避免整段对齐因一个字失败。"""
    if left == right:
        return True
    if not left or not right:
        return False
    # 短文本必须更严格，防止“好”“嗯”等重复气泡被错误对齐。
    if min(len(left), len(right)) <= 3:
        return SequenceMatcher(None, left, right).ratio() >= 0.8
    return SequenceMatcher(None, left, right).ratio() >= 0.72


def _process_single(contact, img, cfg, guard, state, system_prompt,
                    my_name, llm_settings, status) -> str:
    """单聊常驻模式：只看目标聊天页气泡，不使用未读/已读或会话预览。"""
    if img is None:
        return "ignore"

    last_result = "ignore"
    # 一轮最多处理 20 条，避免对方持续发消息时阻塞监控循环太久；
    # 未处理完的消息会在下一轮继续从气泡差异中取出。
    for _ in range(20):
        switched_chat = False
        hwnd = keys.find_wechat_window()
        if not hwnd:
            return last_result
        bubbles, title_ok = watcher.read_chat_bubbles(img, hwnd, contact=contact)
        logging.debug("(%s) 单聊扫描：标题=%s，气泡=%d，已见=%d",
                      contact, title_ok, len(bubbles),
                      len(state.seen_bubbles(contact)))

        if not title_ok:
            logging.info("(%s) 当前不是目标聊天页，尝试切换到该会话", contact)
            if not keys.open_chat(contact, delay=cfg.get("send_delay", 1.2)):
                logging.warning("(%s) 自动切换聊天失败，等待下一轮重试", contact)
                return last_result
            # 配置换人后不能沿用旧联系人留下的气泡游标；切换后的第一帧只建立基线。
            state.set_seen_bubbles(contact, [])
            switched_chat = True
            img = watcher.capture_wechat()
            if img is None:
                return last_result
            bubbles, title_ok = watcher.read_chat_bubbles(img, hwnd, contact=contact)
            if not title_ok:
                logging.warning("(%s) 切换后仍未通过聊天标题校验", contact)
                return last_result

        seen = [] if switched_chat else state.seen_bubbles(contact)
        new_bubbles, all_bubbles = _diff_bubbles(seen, bubbles)
        if not new_bubbles:
            if not seen:      # 首轮建立基线
                state.set_seen_bubbles(contact, [t for _, t in all_bubbles])
                logging.info("基线(单聊): %s 已见气泡 %d 条", contact, len(all_bubbles))
            return last_result

        # 先记录新增的自己消息，直到第一条对方消息为止；这些消息不会触发回复。
        first_them = next((i for i, (side, _text) in enumerate(new_bubbles)
                           if side == "them"), None)
        if first_them is None:
            for _side, text in new_bubbles:
                if text != state.last_sent(contact):
                    state.remember_message(contact, "me", text)
            state.set_seen_bubbles(contact, [t for _, t in all_bubbles])
            return last_result
        for side, text in new_bubbles[:first_them]:
            if side == "me" and text != state.last_sent(contact):
                state.remember_message(contact, "me", text)

        incoming = new_bubbles[first_them][1]
        logging.info("[%s] 新消息(单聊): %s", contact, incoming[:80])
        state.remember_message(contact, "them", incoming)

        text_rows = [t for _, t in all_bubbles]
        base_count = len(text_rows) - len(new_bubbles)
        consumed_end = base_count + first_them + 1

        # 资金/风险类占位（转账/红包/收款码）不交给 AI，转人工处理
        if _is_risk_placeholder(incoming):
            logging.info("(%s) 消息涉及资金/风险(%s)，转人工处理", contact, incoming[:20])
            notify("微信助手-需人工处理", f"{contact}: {incoming[:40]} 未自动回复")
            state.set_seen_bubbles(contact, text_rows[:consumed_end])
            return last_result

        # 后续流程与多人模式完全一致：敏感词/限速护栏 → LLM → 微信发送
        # → 发送后核对 → 记录状态。这里只把“触发条件”换成了气泡 diff。
        result = _generate_and_send(contact, incoming, cfg, guard, state,
                                    system_prompt, my_name, llm_settings,
                                    status, park=False)
        if result == "error":
            return result

        # 只把本次处理到的对方气泡标记为已见；后面的新消息留给下一次迭代。
        state.set_seen_bubbles(contact, text_rows[:consumed_end])
        last_result = result

        # 发送可能产生了新的自己气泡；抓取最新页面后继续处理剩余对方消息。
        img = watcher.capture_wechat()
        if img is None:
            return last_result
    return last_result


# ================= 多人模式：红点 + 预览 =================

def _multi_should_process(contact, row, cfg, state, guard=None):
    """多人模式的触发判断。返回 (是否处理, 生效文本)"""
    badge = row["badge"]
    preview = row["preview"]
    last_preview = state.last_preview(contact)

    # 静默期间保留未读游标，不调用 LLM；时段结束后同一条消息仍可处理。
    if guard is not None and guard.in_quiet_hours():
        return False, preview

    if not badge:
        # 没有未读：更新基线。若之前处理过未读，说明已被读掉，复位
        if state.handled(contact) or last_preview != preview:
            logging.info("(%s) 无未读，基线更新为 %r", contact, preview[:40])
        state.set_last_preview(contact, preview)
        state.set_handled(contact, False)
        return False, preview

    # 有未读：冷却窗口内的同预览视为同一条，避免重复回复
    if (state.handled(contact) and preview == last_preview
            and time.time() - state.handled_ts(contact) < cfg["dedup_ttl"]):
        return False, preview

    if not preview or _no_detail(preview):
        logging.info("(%s) 无可读内容(%s)，不自动回复，转人工", contact, preview)
        notify("微信助手-无法读取内容", f"{contact} 有新消息但内容是 {preview or '空'}，请自行处理")
        state.set_last_preview(contact, preview)
        state.set_handled(contact, True)
        return False, preview
    return True, preview


# ================= 共用：护栏 + LLM + 发送 =================

def _generate_and_send(contact, incoming, cfg, guard, state, system_prompt,
                       my_name, llm_settings, status, park) -> str:
    """护栏 → LLM 生成 → 发送 → 气泡核对 → （多人模式）切离会话。"""
    if guard.in_quiet_hours():
        logging.info("(%s) 当前处于静默时段，不自动回复", contact)
        return "ignore"
    if guard.is_sensitive(incoming):
        logging.info("(%s) 命中敏感词，转人工处理", contact)
        notify("微信助手-需人工处理", f"{contact}: {incoming[:40]} 未自动回复")
        state.set_handled(contact, True)
        return "ignore"
    if guard.over_rate_limit(state, contact):
        logging.info("(%s) 触发每小时限速，不回复", contact)
        state.set_handled(contact, True)
        return "ignore"

    reply = llm.generate_reply(llm_settings, system_prompt, contact,
                               state.memory(contact), my_name,
                               cfg["max_reply_chars"])
    if not reply:
        logging.warning("(%s) LLM 返回空回复，跳过发送", contact)
        return "ignore"

    guard.ui_pace()
    ok = keys.send_reply(contact, reply, delay=cfg.get("send_delay", 1.2))
    if not ok:
        logging.error("(%s) 发送失败", contact)
        return "error"

    # ---- 发送后核对：趁会话开着，在"我的"气泡里找刚发的内容 ----
    # OCR 会把半角"~"读成全角"～"、吞掉空格，先归一化再匹配，
    # 否则消息明明发出去了却一直误报"未能核对"
    def _norm(s: str) -> str:
        return s.replace(" ", "").replace("~", "～").lower()

    frag = _norm(reply)[:8]
    verified = False
    time.sleep(1.0)
    for _ in range(2):
        img = watcher.capture_fresh()
        if img is not None:
            hwnd = keys.find_wechat_window()
            bubbles, _title_ok = watcher.read_chat_bubbles(img, hwnd)
            if any(side == "me" and frag in _norm(t) for side, t in bubbles):
                verified = True
                break
        time.sleep(1.5)

    if park:
        # 多人模式：切离对方会话，保证下一条消息带未读红点
        keys.park_chat(delay=cfg.get("send_delay", 1.2))

    state.remember_message(contact, "me", reply)
    state.record_reply(contact)
    state.set_last_sent(contact, reply)
    state.set_last_preview(contact, reply)
    if status is not None:
        status.add_reply(contact, reply)

    if verified:
        logging.info("[%s] 已回复(已核对): %s", contact, reply[:80])
        state.set_handled(contact, True)
        return "reply"
    logging.warning("(%s) 已执行发送但未能核对到内容，请人工确认", contact)
    notify("微信助手-请人工确认", f"已对 {contact} 执行回复但未能自动核对，请看一眼微信")
    state.set_handled(contact, True)
    return "ignore"


# ================= 入口分发 =================

def process_contact(contact, row, cfg, guard, state, system_prompt,
                    my_name, llm_settings, status=None, img=None) -> str:
    """处理一个监控对象。返回 'reply' | 'ignore' | 'error'"""
    if cfg.get("reply_mode", "multi") == "single":
        return _process_single(contact, img, cfg, guard, state,
                               system_prompt, my_name, llm_settings, status)

    process, text = _multi_should_process(contact, row, cfg, state, guard)
    if not process:
        return "ignore"
    logging.info("[%s] 新消息(未读): %s", contact, text[:80])
    state.remember_message(contact, "them", text)
    state.set_last_preview(contact, text)
    return _generate_and_send(contact, text, cfg, guard, state, system_prompt,
                              my_name, llm_settings, status, park=True)


def run_forever(cfg, guard, state, system_prompt, my_name, llm_settings,
                status=None, stop_event=None, pause_event=None):
    logging.info("开始监控（%s 模式）：%s | 间隔 %ss",
                 cfg.get("reply_mode", "multi"), cfg["watch_list"],
                 cfg.get("ocr_poll", 20))

    # 等待微信就绪并建立基线（最多等 2 分钟；等不到就进循环慢慢等）
    img = None
    for _ in range(12):
        if stop_event is not None and stop_event.is_set():
            return
        img = watcher.capture_wechat()
        if img is not None:
            break
        if _ == 0:
            logging.warning("等待微信窗口就绪…")
        time.sleep(10)
    if img is not None and cfg.get("reply_mode", "multi") == "multi":
        rows = watcher.read_watch_rows(img, cfg["watch_list"])
        for contact, row in rows.items():
            state.set_last_preview(contact, row["preview"])
            state.set_handled(contact, False)
            logging.info("基线: %s | %s | 未读=%s", contact, row["preview"][:40], row["badge"])
    elif img is not None:
        # 单聊模式基线：记录当前已存在的气泡，防止启动后翻旧账
        hwnd = keys.find_wechat_window()
        if hwnd:
            bubbles, title_ok = watcher.read_chat_bubbles(img, hwnd, contact=cfg["watch_list"][0])
            if title_ok:
                state.set_seen_bubbles(cfg["watch_list"][0], [t for _, t in bubbles])
                logging.info("基线(单聊): %s 已见气泡 %d 条", cfg["watch_list"][0], len(bubbles))

    last_heartbeat = time.time()
    consecutive_errors = 0
    last_missing_log = 0.0
    while True:
        try:
            if stop_event is not None and stop_event.is_set():
                logging.info("收到停止信号，监控退出")
                return

            if pause_event is not None and pause_event.is_set():
                if status is not None:
                    status.set_paused(True)
                time.sleep(0.5)
                continue
            if status is not None:
                status.set_paused(False)

            # 配置与人设热重载：改 config.yaml / persona.md 保存即生效
            cfg = load_config()
            guard.cfg = cfg
            system_prompt = build_system_prompt(load_persona())
            if status is not None:
                status.set_watch_list(cfg["watch_list"])

            img = watcher.capture_wechat()
            if status is not None:
                status.set_wechat_ok(img is not None)
            if img is None:
                # 微信未启动/重启中是正常状态，等待即可，不计入熔断
                if time.time() - last_missing_log > 60:
                    logging.warning("微信窗口不可用，等待微信登录…")
                    last_missing_log = time.time()
                _stop_wait(cfg.get("ocr_poll", 20), stop_event)
                continue

            single_mode = cfg.get("reply_mode", "multi") == "single"
            contacts = cfg["watch_list"][:1] if single_mode else cfg["watch_list"]
            if single_mode and len(cfg["watch_list"]) > 1:
                logging.warning("单聊模式只使用监控名单中的第一个联系人")
            # 单聊模式完全基于当前聊天页气泡，不读取会话列表/红点/预览。
            rows = {} if single_mode else watcher.read_watch_rows(img, cfg["watch_list"])
            for contact in contacts:
                row = rows.get(contact)
                if not single_mode and row is None:
                    continue    # 会话不在可见列表里（太旧被顶出），跳过
                result = process_contact(contact, row, cfg, guard, state,
                                         system_prompt, my_name, llm_settings,
                                         status=status, img=img)
                if result == "error":
                    consecutive_errors += 1
                    if consecutive_errors >= cfg["max_consecutive_errors"]:
                        logging.error("连续出错达到上限，熔断停止")
                        notify("微信助手已停止", "发送连续失败触发熔断，请查看日志")
                        if status is not None:
                            status.set_running(False)
                            status.set_last_error("连续发送失败触发熔断，已自动停止")
                        return
                else:
                    consecutive_errors = 0
                    guard.tick_ok()

            if time.time() - last_heartbeat > 300:
                logging.info("心跳：OCR 轮询运行中")
                last_heartbeat = time.time()
            _stop_wait(cfg.get("ocr_poll", 20), stop_event)
        except KeyboardInterrupt:
            logging.info("收到 Ctrl+C，退出")
            return
        except Exception as e:
            logging.exception("轮询异常: %s", e)
            consecutive_errors += 1
            if consecutive_errors >= cfg["max_consecutive_errors"]:
                logging.error("连续出错达到上限，熔断停止")
                notify("微信助手已停止", "连续出错触发熔断，请查看日志")
                if status is not None:
                    status.set_running(False)
                    status.set_last_error("连续出错触发熔断，已自动停止")
                return
            time.sleep(cfg.get("ocr_poll", 20))


def _stop_wait(seconds, stop_event):
    if stop_event is not None:
        stop_event.wait(seconds)
    else:
        time.sleep(seconds)
