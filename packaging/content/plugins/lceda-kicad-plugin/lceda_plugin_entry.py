#!/usr/bin/env python3
"""KiCad API 插件入口:点击 eeschema 工具栏图标时由 KiCad 直接启动本脚本
(KICAD_API_SOCKET / KICAD_API_TOKEN 环境变量已注入,GUI 内的 kipy 自动连接)。

自包含:主代码(lceda_plugin/)与 kipy(IPC 绑定)均随包分发,无外部路径依赖。

依赖解析顺序:
  1. KiCad 创建的插件 venv(--system-site-packages,可见系统级 PySide6 等)
  2. 用户 site-packages(~/.local/lib/pythonX.Y/...)
  3. 都找不到时写错误日志并退出(不自动 pip 安装,避免卡在"未就绪"状态;
     缺失依赖按提示手动安装即可)
"""

import os
import site
import sys

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)


def _extend_paths() -> None:
    """让系统级/用户级包在插件 venv 内可见。"""
    try:
        user_sp = site.getusersitepackages()
    except Exception:  # noqa: BLE001
        user_sp = ""
    if user_sp and user_sp not in sys.path and os.path.isdir(user_sp):
        sys.path.append(user_sp)


def _missing() -> list[str]:
    need = []
    for mod in ("PySide6", "httpx", "cryptography", "kipy"):
        try:
            __import__(mod)
        except ImportError:
            need.append(mod)
    return need


def main() -> int:
    _extend_paths()
    missing = _missing()
    if missing:
        pip_names = " ".join(missing)
        err = (f"缺少依赖: {pip_names}\n"
               f"安装: pip install --user {pip_names}\n"
               "注: kipy(kicad-python)已随本插件分发,若提示缺失说明安装目录异常。")
        print(f"lceda-kicad-plugin: {err}", file=sys.stderr)
        try:
            with open(os.path.join(PLUGIN_DIR, "lceda-plugin-error.log"),
                      "a", encoding="utf-8") as f:
                f.write(err + "\n")
        except OSError:
            pass
        return 1

    from lceda_plugin.main import main as app_main
    return app_main()


if __name__ == "__main__":
    raise SystemExit(main())
