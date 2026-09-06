# -*- coding: utf-8 -*-
"""一键构建 Windows 桌面应用。

用法：python build_app.py
产物：dist/微信自动回复助手/  （整个文件夹压缩后即可分发）
说明：重建时自动保留已填好的 .env（分发给别人前记得清空里面的 Key）。
"""
import shutil
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "微信自动回复助手"
ICON_FILE = ROOT / "assets" / "app.ico"


def ensure_packages():
    """确保构建依赖就绪"""
    import importlib
    name_for = {"PyInstaller": "pyinstaller", "customtkinter": "customtkinter",
                "pystray": "pystray"}
    missing = []
    for mod in ("PyInstaller", "customtkinter", "pystray"):
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(name_for[mod])
    if missing:
        print(f"安装缺失依赖: {missing}")
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "--default-timeout=120", *missing], check=True)

    # PyInstaller 的 tkinter hook 会在 Tk 运行时不完整时静默排除 tkinter，
    # 最终产出一个双击即弹 Unhandled exception 的 EXE；构建前直接给出明确错误。
    try:
        import tkinter
        tkinter.Tcl().eval("info patchlevel")
    except Exception as exc:
        raise SystemExit(
            "当前 Python 的 Tk/Tcl 安装不可用，无法构建 GUI。"
            "请安装带 Tcl/Tk 的标准 Python 后重试。"
        ) from exc


def make_icon():
    """用 PIL 生成橙底猫耳"喵"字图标（多尺寸 ico）"""
    from PIL import Image, ImageDraw, ImageFont
    ICON_FILE.parent.mkdir(exist_ok=True)
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle([6 * s, 14 * s, 58 * s, 58 * s], radius=14 * s,
                        fill=(245, 166, 35, 255))
    d.polygon([(12 * s, 26 * s), (22 * s, 6 * s), (30 * s, 22 * s)],
              fill=(245, 166, 35, 255))
    d.polygon([(52 * s, 26 * s), (42 * s, 6 * s), (34 * s, 22 * s)],
              fill=(245, 166, 35, 255))
    font = None
    for path in ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
                 "C:/Windows/Fonts/simhei.ttf"):
        try:
            font = ImageFont.truetype(path, int(26 * s))
            break
        except OSError:
            continue
    d.text((32 * s, 37 * s), "喵", font=font, fill=(255, 255, 255, 255), anchor="mm")
    img.save(ICON_FILE, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                               (64, 64), (128, 128), (256, 256)])
    print(f"图标已生成: {ICON_FILE}")


def find_existing_env_text() -> str:
    """在所有 dist* 产物里找已填好的 .env（重建时不丢用户配置）"""
    for d in sorted(ROOT.glob("dist*")):
        f = d / APP_NAME / ".env"
        if f.exists():
            with suppress(Exception):
                if f.read_text(encoding="utf-8").strip():
                    return f.read_text(encoding="utf-8")
    return ""


# 构建会清空 dist，这些是使用者在应用目录里的运行数据，重建前必须抢救
USER_FILES = ("config.yaml", "persona.md", ".env", "state.json")


def stash_user_files() -> dict:
    """把 dist 应用目录里用户的运行配置读进内存，构建完成后原样放回。"""
    stashed = {}
    for base in (ROOT / "dist" / APP_NAME, ROOT / "dist"):
        for name in USER_FILES:
            if name in stashed:
                continue
            p = base / name
            if p.exists():
                with suppress(Exception):
                    text = p.read_text(encoding="utf-8")
                    if text.strip():
                        stashed[name] = text
    if ".env" not in stashed:
        text = find_existing_env_text()
        if text:
            stashed[".env"] = text
    return stashed


def pick_dist_path() -> Path:
    """选一个可用的输出目录：dist 被占用（如资源管理器停在目录里）就换 dist_2/3…"""
    for i in range(1, 10):
        candidate = ROOT / ("dist" if i == 1 else f"dist_{i}")
        if not candidate.exists():
            return candidate
        try:
            shutil.rmtree(candidate)
            return candidate
        except PermissionError:
            print(f"{candidate.name} 被其他程序占用，换下一个目录…")
    sys.exit("dist/dist_2…dist_9 全部被占用，请手动清理后重试")


def run_pyinstaller(dist_path: Path):
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",                 # 无控制台黑框
        "--onedir",                   # 文件夹版：启动快、误报少
        "--distpath", str(dist_path),
        "--name", APP_NAME,
        "--icon", str(ICON_FILE),
        # 打包 OCR 模型、onnxruntime 运行库、CustomTkinter 主题资源
        "--collect-all", "rapidocr_onnxruntime",
        "--collect-all", "customtkinter",
        "--hidden-import", "pystray._win32",
        str(ROOT / "app.py"),
    ]
    print("PyInstaller 构建中（首次约 3~8 分钟）…")
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        sys.exit("PyInstaller 构建失败，请把上方报错发给开发者")


def copy_configs(dist_path: Path, stashed: dict):
    """把配置写进【应用目录】dist_path/APP_NAME（exe 旁边的便携目录）。
    使用者已有的运行配置优先；缺什么补项目默认什么。"""
    app_dir = dist_path / APP_NAME
    if not app_dir.exists():
        sys.exit(f"未找到产物目录 {app_dir}")
    # 1) 用户运行配置优先：监控名单/人设/Key/聊天记录重建不丢
    for name, text in stashed.items():
        with suppress(Exception):
            (app_dir / name).write_text(text, encoding="utf-8")
    # 2) 缺失的补默认
    for f in ("README.md", ".env.example"):
        src = ROOT / f
        if src.exists() and not (app_dir / f).exists():
            shutil.copy2(src, app_dir / f)
    if not (app_dir / "config.yaml").exists():
        shutil.copy2(ROOT / "config.yaml", app_dir / "config.yaml")
    if not (app_dir / "persona.md").exists():
        shutil.copy2(ROOT / "persona.md", app_dir / "persona.md")
    if not ((app_dir / ".env").exists()
            and (app_dir / ".env").read_text(encoding="utf-8").strip()):
        (app_dir / ".env").write_text(
            "LLM_BASE_URL=\nLLM_API_KEY=\nLLM_MODEL=\n", encoding="utf-8")
        print("已写入空 .env 模板")
    else:
        print("已保留使用者 .env / 监控配置 / 人设（分发给别人前记得清空 Key）")
    # 防呆校验：三件套缺一不可
    missing = [f for f in ("config.yaml", "persona.md", ".env")
               if not (app_dir / f).exists()]
    if missing:
        sys.exit(f"构建产物缺少 {missing}，请检查")


def main():
    ensure_packages()
    make_icon()
    stashed = stash_user_files()
    dist_path = pick_dist_path()
    run_pyinstaller(dist_path)
    copy_configs(dist_path, stashed)
    size_mb = sum(f.stat().st_size for f in dist_path.rglob("*") if f.is_file()) / 1e6
    print()
    print("=" * 56)
    print(f"构建完成: {dist_path}")
    print(f"体积约 {size_mb:.0f} MB。把整个文件夹压缩后即可分发。")
    print("使用者解压后双击「微信自动回复助手.exe」，")
    print("在【设置 → AI 接口】填入自己的 API Key 即可使用。")
    print("提示：首次运行如遇 SmartScreen 蓝色提示，点「更多信息→仍要运行」。")
    print("=" * 56)


if __name__ == "__main__":
    main()
