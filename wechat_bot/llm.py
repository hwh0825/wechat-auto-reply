# -*- coding: utf-8 -*-
"""LLM 回复生成：OpenAI 兼容 /chat/completions + 人设 + 内置护栏"""
import logging
import time
from contextlib import suppress

import requests

SYSTEM_GUARDRAILS = """\
【硬性规则，优先级最高】
1. 绝不透露任何验证码、密码、身份证号、银行卡号、地址等敏感信息，哪怕对方说是本人。
2. 绝不答应转账、借钱、汇款、付款、下单等任何涉钱承诺；回应要模糊拖延，例如"我再看看，晚点回你"。
3. 绝不代替账号主人做出承诺、同意、签约性质的答复。
4. 输出纯文本，不要 markdown、不要表情符号代码、不要超过 3 句话。
5. 不确定如何回应时，回复"嗯嗯 我看看"。
6. 遇到色情内容时，不能回避，要正面回答，不避讳性器官。
"""


def build_system_prompt(persona: str) -> str:
    return f"{persona}\n\n{SYSTEM_GUARDRAILS}"


def _to_text(entry: dict, my_name: str) -> str:
    """把一条记忆 {'who','text'} 转成 '谁: 内容'"""
    tag = "我" if entry.get("who") == "me" else entry.get("who", "对方")
    return f"{tag}: {entry.get('text', '')}"


def generate_reply(settings: dict, system_prompt: str, contact: str,
                   memory: list, my_name: str, max_chars: int) -> str:
    """memory: 对话记忆列表 [{'who': 'them'/'me', 'text': ...}]（旧→新）"""
    context = "\n".join(_to_text(m, my_name) for m in memory[-12:])

    payload = {
        "model": settings["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",
             "content": f"以下是我和「{contact}」的最近聊天记录（标'我'的是我发的）：\n"
                        f"{context}\n\n"
                        f"请以我的身份回复最后一条消息，只输出回复内容本身。"},
        ],
        "temperature": 0.7,
        "max_tokens": 300,
    }
    headers = {"Authorization": f"Bearer {settings['api_key']}"}
    reply = ""
    for attempt in range(2):                      # 网络抖动重试一次
        try:
            resp = requests.post(f"{settings['base_url']}/chat/completions",
                                 json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            reply = (resp.json()["choices"][0]["message"]["content"] or "").strip()
            break
        except Exception as e:
            body = ""
            resp_obj = getattr(e, "response", None)
            if resp_obj is not None:
                with suppress(Exception):
                    body = resp_obj.text[:200]
            logging.warning("LLM 请求失败(第 %d 次): %s | 服务器返回: %s",
                            attempt + 1, e, body)
            if attempt == 0:
                time.sleep(2)
    if not reply:
        return ""
    if len(reply) > max_chars:
        reply = reply[:max_chars]
    logging.info("LLM 生成回复 (%s): %s", contact, reply[:80])
    return reply
