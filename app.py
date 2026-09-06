# -*- coding: utf-8 -*-
"""打包入口：微信自动回复助手 GUI 应用（PyInstaller 目标文件）"""
import os
import shutil
import sys
import tempfile
from pathlib import Path


def _bootstrap_tk_resources():
    """让冻结版 Tk 在中文安装路径下也能找到 Tcl/Tk 脚本资源。

    Tcl 的部分 Windows 构建仍会用本地代码页解析资源路径；PyInstaller
    默认把资源放在包含中文应用名的 ``_internal`` 目录，导致 init.tcl
    明明存在却被 Tcl 报为找不到。复制到临时 ASCII 目录可绕过这个限制。
    """
    if not getattr(sys, "frozen", False):
        return
    bundle = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    # 新旧 PyInstaller hook 的目录布局不同：有的版本使用
    # ``_tcl_data``，有的版本保留标准的 ``tcl/tcl8.6`` 层级。
    tcl_src = bundle / "tcl" / "tcl8.6"
    tk_src = bundle / "tcl" / "tk8.6"
    if not (tcl_src / "init.tcl").exists():
        tcl_src = bundle / "_tcl_data"
        tk_src = bundle / "_tk_data"
    if not (tcl_src / "init.tcl").exists():
        return
    # 使用固定目录，避免每次启动重复复制；临时目录由系统清理即可。
    target = Path(tempfile.gettempdir()) / "wechat_auto_reply_tk"
    tcl_dst = target / "tcl8.6"
    tk_dst = target / "tk8.6"
    try:
        if not (tcl_dst / "init.tcl").exists():
            shutil.copytree(tcl_src, tcl_dst, dirs_exist_ok=True)
        if tk_src.exists() and not (tk_dst / "tk.tcl").exists():
            shutil.copytree(tk_src, tk_dst, dirs_exist_ok=True)
        os.environ["TCL_LIBRARY"] = str(tcl_dst)
        os.environ["TK_LIBRARY"] = str(tk_dst)
    except OSError:
        # 资源复制失败时保留 PyInstaller 默认路径，让程序给出原生错误。
        pass


_bootstrap_tk_resources()

from wechat_bot.app_gui import main

if __name__ == "__main__":
    main()
