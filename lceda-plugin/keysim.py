"""跨 Wayland/X11 的按键模拟(放置后自动进入移动模式)。

Wayland 安全模型禁止普通进程注入全局按键;仅在系统配置了 ydotool
(uinput 内核接口,需要 root 或 udev 放行)时可用。X11 会话回退 xdotool。

返回 (是否成功, 描述):失败时调用方向用户展示"按 M"的手动提示。
"""

from __future__ import annotations

import shutil
import subprocess


def _x11_send_m() -> tuple[bool, str]:
    xdotool = shutil.which("xdotool")
    if not xdotool:
        return False, "xdotool 未安装"
    try:
        subprocess.run(
            [xdotool, "search", "--onlyvisible", "--class", "kicad",
             "windowactivate", "--sync", "key", "m"],
            check=True, capture_output=True, timeout=5)
        return True, "x11"
    except (subprocess.SubprocessError, OSError) as ex:
        return False, f"xdotool: {ex}"


def _wayland_send_m() -> tuple[bool, str]:
    ydotool = shutil.which("ydotool")
    if not ydotool:
        return False, "ydotool 未安装"
    # KEY_M = 50(keycode);按下与释放
    try:
        subprocess.run([ydotool, "key", "50:1", "50:0"],
                       check=True, capture_output=True, timeout=5)
        return True, "ydotool"
    except (subprocess.SubprocessError, OSError) as ex:
        return False, f"ydotool: {ex}"


def send_move_key(session_type: str) -> tuple[bool, str]:
    """向 KiCad 发送 M 键(选中符号时进入跟随鼠标的移动模式)。"""
    if session_type == "x11":
        ok, how = _x11_send_m()
        if ok:
            return True, how
    else:
        ok, how = _wayland_send_m()
        if ok:
            return True, how
        if shutil.which("xdotool") and session_type not in ("wayland",):
            return _x11_send_m()
    return False, how
