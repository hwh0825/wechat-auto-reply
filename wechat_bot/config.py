# -*- coding: utf-8 -*-
"""配置加载：config.yaml + .env，每轮循环热重载"""
import logging
import os
import sys
from contextlib import suppress
from pathlib import Path

import yaml

# 打包成 exe 后，配置/日志/人设都放在 exe 旁边（便携式）；
# 源码运行时则放在项目根目录
if getattr(sys, "frozen", False):
    PROJECT_DIR = Path(sys.executable).resolve().parent
else:
    PROJECT_DIR = Path(__file__).resolve().parent.parent

CONFIG_FILE = PROJECT_DIR / "config.yaml"
PERSONA_FILE = PROJECT_DIR / "persona.md"
ENV_FILE = PROJECT_DIR / ".env"
STATE_FILE = PROJECT_DIR / "state.json"
LOG_DIR = PROJECT_DIR / "logs"
LOCK_FILE = PROJECT_DIR / "bot.lock"


def _load_env():
    """极简 .env 解析（避免引入 python-dotenv 依赖）"""
    env = {}
    if ENV_FILE.exists():
        try:
            lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            logging.getLogger(__name__).warning("读取 .env 失败: %s", exc)
            return env
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


_ENV_TEMPLATE = """\
LLM_BASE_URL={base_url}
LLM_API_KEY={api_key}
LLM_MODEL={model}
"""


def load_llm_settings(fatal: bool = False):
    """读取 LLM 配置。返回 (settings_dict 或 None, 错误信息)。
    fatal=True 时缺配置直接退出（控制台模式用）。"""
    env = _load_env()
    base = env.get("LLM_BASE_URL", "").rstrip("/")
    key = env.get("LLM_API_KEY", "")
    model = env.get("LLM_MODEL", "")
    missing = [k for k, v in
               [("LLM_BASE_URL", base), ("LLM_API_KEY", key), ("LLM_MODEL", model)] if not v]
    if missing:
        msg = f"缺少配置: {', '.join(missing)}"
        if fatal:
            sys.exit(f"[配置缺失] {msg}（.env.example 有说明）")
        return None, msg
    return {"base_url": base, "api_key": key, "model": model}, ""


def save_llm_settings(base_url: str, api_key: str, model: str):
    """界面保存 AI 接口配置 → 写 .env"""
    _atomic_write_text(ENV_FILE, _ENV_TEMPLATE.format(
        base_url=base_url.strip().rstrip("/"),
        api_key=api_key.strip(),
        model=model.strip()))


def load_config_raw() -> dict:
    """读取 config.yaml 原始内容。文件缺失/损坏时自动生成默认配置文件并返回默认值，
    保证监控线程永远不会因配置缺失而崩溃。"""
    if not CONFIG_FILE.exists():
        _write_default_config()
        return _default_cfg()
    data = None
    with suppress(Exception):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    if not isinstance(data, dict):
        # 文件缺失有效内容（解析失败/为空/是标量）：备份坏文件，重新生成
        with suppress(Exception):
            bad = CONFIG_FILE.with_suffix(".yaml.broken")
            if bad.exists():
                bad.unlink()
            CONFIG_FILE.rename(bad)
            logging.getLogger(__name__).warning(
                "config.yaml 无效，已备份为 %s 并重新生成默认配置", bad.name)
        _write_default_config()
        return _default_cfg()
    return data


def _default_cfg() -> dict:
    return {
        "watch_list": [],
        "ocr_poll": 20,
        "reply_mode": "multi",
        "min_ui_interval": 5,
        "max_replies_per_hour": 0,
        "max_reply_chars": 300,
        "dedup_ttl": 180,
        "quiet_hours": [],
        "sensitive_keywords": ["转账", "红包", "借钱", "验证码"],
        "max_consecutive_errors": 5,
    }


_DEFAULT_CFG_TEXT = """\
# 微信自动回复助手配置（程序在配置缺失/损坏时自动生成）
# 改完保存即可，脚本每轮循环会自动重新加载

# 监控对象：好友昵称/备注名，必须和微信会话列表显示一致，仅支持私聊
watch_list:
  - "文件传输助手"

reply_mode: multi          # single=单聊常驻（停在聊天页，句句有回应）｜multi=多人轮询
ocr_poll: 20               # 检测间隔（秒）
send_delay: 1.2            # 键盘操作间隔（秒）
min_ui_interval: 5         # 两次发送的最小间隔（秒）
max_replies_per_hour: 0    # 单联系人每小时回复上限；0 = 不限制
max_reply_chars: 300       # 单条回复最大长度
dedup_ttl: 180             # 同预览未读的去重冷却（秒）
quiet_hours: []            # 静默时段，如 ["02:00", "07:30"]；空 = 全天

sensitive_keywords:        # 命中不代回，弹窗转人工
  - "转账"
  - "红包"
  - "借钱"
  - "验证码"

max_consecutive_errors: 5  # 连续出错 N 次自动停止
"""


def _write_default_config():
    with suppress(Exception):
        CONFIG_FILE.write_text(_DEFAULT_CFG_TEXT, encoding="utf-8")


def save_config_values(cfg: dict):
    """界面保存设置 → 写 config.yaml"""
    text = yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)
    _atomic_write_text(CONFIG_FILE, text)


def _atomic_write_text(path: Path, text: str):
    """先完整写入同目录临时文件，再替换目标，避免热读读到半截配置。"""
    temp = path.with_name(f".{path.name}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
    finally:
        with suppress(OSError):
            if temp.exists():
                temp.unlink()


def load_config() -> dict:
    # 使用与 GUI/监控线程相同的容错读取逻辑，避免热重载时遇到半截或损坏
    # 文件直接把监控线程打崩。
    cfg = load_config_raw()
    if not isinstance(cfg, dict):
        cfg = _default_cfg()
    # 默认值兜底
    watch_list = cfg.get("watch_list", [])
    if not isinstance(watch_list, list):
        watch_list = []
    cfg["watch_list"] = [str(n).strip() for n in watch_list if str(n).strip()]
    cfg.setdefault("ocr_poll", 20)
    cfg.setdefault("reply_mode", "multi")
    if cfg["reply_mode"] not in ("single", "multi"):
        cfg["reply_mode"] = "multi"
    def _number(key, default, minimum=None, cast=float):
        try:
            value = cast(cfg.get(key, default))
        except (TypeError, ValueError):
            value = cast(default)
            logging.getLogger(__name__).warning(
                "配置 %s 无效，已使用默认值 %s", key, default)
        if minimum is not None and value < minimum:
            value = minimum
        cfg[key] = value

    _number("min_ui_interval", 5, minimum=0.0, cast=float)
    _number("max_replies_per_hour", 10, minimum=0, cast=int)
    _number("max_reply_chars", 300, minimum=1, cast=int)
    _number("dedup_ttl", 180, minimum=0.0, cast=float)
    _number("max_consecutive_errors", 5, minimum=1, cast=int)

    quiet = cfg.get("quiet_hours", [])
    if not isinstance(quiet, list) or len(quiet) != 2:
        cfg["quiet_hours"] = []
    else:
        cfg["quiet_hours"] = [str(v).strip() for v in quiet]

    sensitive = cfg.get("sensitive_keywords", [])
    if not isinstance(sensitive, list):
        sensitive = []
    cfg["sensitive_keywords"] = [str(v).strip() for v in sensitive if str(v).strip()]
    if not cfg["watch_list"]:
        sys.exit("[配置缺失] config.yaml 的 watch_list 不能为空")
    _number("ocr_poll", 20, minimum=5, cast=int)   # 检测间隔下限 5 秒，防止过猛
    return cfg


def load_persona() -> str:
    if PERSONA_FILE.exists():
        return PERSONA_FILE.read_text(encoding="utf-8").strip()
    return "你代替账号主人回复微信消息，语气自然随意，每条不超过三句话。"
