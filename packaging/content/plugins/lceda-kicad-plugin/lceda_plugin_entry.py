#!/usr/bin/env python3
"""KiCad API 插件入口:点击 eeschema 工具栏图标时由 KiCad 直接启动本脚本
(KICAD_API_SOCKET / KICAD_API_TOKEN 环境变量已注入,GUI 内的 kipy 自动连接)。

主代码查找顺序(先命中先用):
  1. 本目录下的 lceda_plugin/          ← PCM 自包含安装
  2. 开发工作区 lceda-plugin/          ← 目录名含连字符,运行时注册为虚拟包
  3. 开发工作区 lceda_plugin/          ← 旧目录名回退
"""

import os
import site
import sys
import types
from pathlib import Path

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DEV_ROOT = "/home/juno/tools/kicad-jlc-dfm"


def _extend_paths() -> None:
    """让系统级/用户级包(如 venv 外的 PySide6)可见。"""
    try:
        user_sp = site.getusersitepackages()
    except Exception:  # noqa: BLE001
        user_sp = ""
    if user_sp and user_sp not in sys.path and os.path.isdir(user_sp):
        sys.path.append(user_sp)


def _register_package(base: Path) -> bool:
    """把 base 下的主代码目录注册为可导入的 lceda_plugin 包。"""
    underscore = base / "lceda_plugin"
    hyphen = base / "lceda-plugin"

    if underscore.is_dir():
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))
        return True

    if hyphen.is_dir():
        # 连字符目录无法被普通 import,注册为虚拟包
        pkg = types.ModuleType("lceda_plugin")
        pkg.__path__ = [str(hyphen)]
        sys.modules["lceda_plugin"] = pkg
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))
        return True

    return False


def _resolve_package() -> bool:
    for base in (Path(PLUGIN_DIR), Path(DEV_ROOT)):
        if _register_package(base):
            return True
    return False


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
    if not _resolve_package():
        err = "找不到主代码(lceda_plugin/ 或 lceda-plugin/)"
        print(f"lceda-kicad-plugin: {err}", file=sys.stderr)
        try:
            with open(os.path.join(PLUGIN_DIR, "lceda-plugin-error.log"),
                      "a", encoding="utf-8") as f:
                f.write(err + "\n")
        except OSError:
            pass
        return 1

    missing = _missing()
    if missing:
        pip_names = " ".join(missing)
        err = f"缺少依赖: {pip_names}\n安装: pip install --user {pip_names}"
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
