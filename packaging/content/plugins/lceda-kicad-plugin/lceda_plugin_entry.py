#!/usr/bin/env python3
"""KiCad API 插件入口:点击 eeschema 工具栏图标时由 KiCad 直接启动本脚本
(KICAD_API_SOCKET / KICAD_API_TOKEN 环境变量已注入,GUI 内的 kipy 自动连接)。

主代码查找顺序(先命中先用):
  1. 本目录下的 lceda_plugin/          ← PCM 自包含安装
  2. 开发工作区 lceda-plugin/          ← 目录名含连字符,运行时注册为虚拟包
  3. 开发工作区 lceda_plugin/          ← 旧目录名回退

依赖引导:PCM 无法在安装时执行脚本,且 KiCad 官方 requirements.txt
机制在缺 python3-venv 的系统上会因 venv 无 pip 而失败(插件不加载,
故不采用),因此由本入口在首次运行时自动 `pip install` 补齐
(PySide6 约 200 MB,需联网,可能持续数分钟),装完直接继续启动,
无需重启 KiCad。在 KiCad venv 内运行时直接装进 venv(--user 的包
venv 不可见);裸 Python 下按 --user → PEP 668 回退。kipy 随包内置,
其依赖(pynng/protobuf/zstandard/jsonschema)缺失时经 kicad-python
一并补齐。

pip 来源:-m pip → ensurepip 引导 → 随包分发的官方 zipapp pip.pyz
(KiCad 内置/精简 Python 常无 pip 模块,build_pcm.py 构建时自动下载)。
设 LCEDA_NO_AUTOINSTALL=1 关闭自动安装;
全过程日志见本目录 lceda-plugin-error.log。
"""

from __future__ import annotations

import importlib
import os
import site
import subprocess
import sys
import types
from pathlib import Path

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DEV_ROOT = "/home/juno/tools/kicad-jlc-dfm"
LOG_FILE = os.path.join(PLUGIN_DIR, "lceda-plugin-error.log")
INSTALL_TIMEOUT = 1800  # PySide6 较大,慢网放宽到 30 分钟

# 顶层 import 名 → pip 安装清单。kipy 必须列出其全部运行时依赖:
# PyPI 的 kicad-python(0.8.0)不声明 zstandard,而 bundled kipy
# (0.9.0 fork)import 需要它——只装 kicad-python 会陷入
# "pip 已满足却仍缺依赖"的死循环(版本约束与 fork 的 pyproject 一致)。
REQUIRED: dict[str, list[str]] = {
    "PySide6": ["PySide6"],
    "httpx": ["httpx"],
    "cryptography": ["cryptography"],
    "kipy": ["kicad-python", "zstandard>=0.25,<0.26",
             "protobuf>=5.29.6,<6", "pynng>=0.9,<0.10",
             "jsonschema>=4.23,<5", "typing_extensions>=4.13.2"],
}


def _log(msg: str) -> None:
    """追加日志(KiCad 启动插件时 stdout 不可见,排障全靠此文件)。"""
    try:
        from datetime import datetime

        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


def _log_env() -> None:
    """记录 KiCad 实际使用的 Python 环境,便于诊断依赖安装问题。"""
    try:
        user_sp = site.getusersitepackages()
    except Exception:  # noqa: BLE001
        user_sp = "?"
    _log(f"python={sys.executable or '?'} "
         f"version={sys.version.split()[0]} "
         f"user_site={user_sp} "
         f"user_site_enabled={getattr(site, 'ENABLE_USER_SITE', '?')}")


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


def _missing(*, log: bool = True) -> list[str]:
    """探测缺失依赖,返回待安装的 pip 发行名列表(已去重)。

    import 失败的根因可能是传递依赖(如 bundled kipy 缺 pynng),
    统一映射回顶层包的发行名,由 pip 补齐整棵依赖树。
    """
    need: list[str] = []
    for mod, pip_names in REQUIRED.items():
        try:
            __import__(mod)
        except ImportError as exc:  # ModuleNotFoundError 及链式失败
            root = getattr(exc, "name", "") or mod
            if log:
                _log(f"import {mod} 失败(根因缺 {root}):{exc}")
            for pip_name in pip_names:
                if pip_name not in need:
                    need.append(pip_name)
    return need


def _probe(exe: str, *args: str) -> bool:
    try:
        proc = subprocess.run([exe, *args],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _pip_launcher(exe: str) -> list[str] | None:
    """返回可用的 pip 调用前缀;两者皆无则 None。

    顺序:-m pip → 随包分发的 pip.pyz(官方 zipapp,任何 Python 3.8+
    可直接运行,专为无 pip 的内置/精简 Python 设计)。
    """
    if _probe(exe, "-m", "pip", "--version"):
        return [exe, "-m", "pip"]
    pyz = os.path.join(PLUGIN_DIR, "pip.pyz")
    if os.path.isfile(pyz) and _probe(exe, pyz, "--version"):
        _log("pip 模块不可用,使用随包分发的 pip.pyz")
        return [exe, pyz]
    return None


def _ensure_pip(exe: str) -> list[str] | None:
    """确认 pip 可用;缺失时先试 ensurepip 引导,再探包内 pip.pyz。"""
    launcher = _pip_launcher(exe)
    if launcher:
        return launcher
    _log("pip 不可用,尝试 ensurepip 引导 …")
    try:
        subprocess.run([exe, "-m", "ensurepip", "--upgrade"],
                       capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return _pip_launcher(exe)


def _pip_install(launcher: list[str], pkgs: list[str]) -> bool:
    """pip --user 安装;按 PEP 668 / KiCad 内置 Python 场景逐级回退。

    国内网络可设 LCEDA_PIP_INDEX_URL 走镜像
    (如 https://pypi.tuna.tsinghua.edu.cn/simple)。
    """
    common = ["install", "--no-input", "--disable-pip-version-check",
              "--timeout", "60"]
    index = os.environ.get("LCEDA_PIP_INDEX_URL")
    if index:
        common += ["--index-url", index]

    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if in_venv:
        # KiCad 专属 venv(python-environments/<identifier>):直装进
        # venv——--user 的包 venv 看不见;venv 也不受 PEP 668 限制
        variants = [[*launcher, *common, *pkgs]]
    else:
        variants = [
            [*launcher, *common, "--user", *pkgs],
            # Debian/Ubuntu 23+/Fedora:externally-managed-environment
            [*launcher, *common, "--user", "--break-system-packages", *pkgs],
            # KiCad 内置 Python(Windows per-user 安装):直接装进其 site-packages
            [*launcher, *common, "--break-system-packages", *pkgs],
        ]
    for cmd in variants:
        _log("$ " + " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=INSTALL_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log(f"pip 运行异常: {exc}")
            continue
        if proc.returncode == 0:
            return True
        out = ((proc.stderr or "") + (proc.stdout or "")).strip()
        _log(f"pip 失败(rc={proc.returncode}):{out[-2000:]}")
    return False


def _notify(msg: str, windows_modal: bool = False) -> None:
    """桌面通知(安装耗时数分钟,必须有可见状态,否则像卡死)。

    Linux: notify-send;macOS: osascript;Windows: ctypes 弹窗
    (标准库,无第三方依赖)。任何失败静默,只写日志。
    """
    _log(msg)
    try:
        if os.name == "nt":
            if windows_modal:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    0, msg, "LCSC → KiCad 插件", 0x40)  # MB_ICONINFORMATION
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["osascript", "-e",
                 f'display notification "{msg}" with title "LCSC → KiCad"'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["notify-send", "LCSC → KiCad 插件", msg],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        pass


def _bootstrap(pip_pkgs: list[str]) -> bool:
    exe = sys.executable or "python3"
    _notify(f"首次运行:正在安装 Python 依赖({' '.join(pip_pkgs)},"
            "约 200 MB,需联网,可能持续数分钟,请保持网络畅通…)")
    launcher = _ensure_pip(exe)
    if not launcher:
        _notify("pip/ensurepip/pip.pyz 均不可用,自动安装中止,"
                f"请手动安装依赖,详情见 {LOG_FILE}", windows_modal=True)
        return False
    ok = _pip_install(launcher, pip_pkgs)
    if ok:
        # Windows 用 GUI 弹出作为完成指示,不再额外弹窗
        if os.name != "nt":
            _notify("依赖安装完成,正在启动界面…")
    else:
        _notify("依赖自动安装失败,请按日志中 pip 报错手动处理,"
                f"日志: {LOG_FILE}", windows_modal=True)
    return ok


def main() -> int:
    _extend_paths()
    if not _resolve_package():
        err = "找不到主代码(lceda_plugin/ 或 lceda-plugin/)"
        _log(err)
        print(f"lceda-kicad-plugin: {err}", file=sys.stderr)
        return 1

    missing = _missing()
    if missing:
        _log_env()
    if missing and not os.environ.get("LCEDA_NO_AUTOINSTALL"):
        if _bootstrap(missing):
            # 安装后 user site 目录才存在,重新纳入 sys.path 并刷新导入
            _extend_paths()
            importlib.invalidate_caches()
            missing = _missing(log=False)

    if missing:
        pip_names = " ".join(missing)
        err = (f"缺少依赖: {pip_names}\n"
               f"安装: pip install --user {pip_names}\n"
               f"详情见日志: {LOG_FILE}")
        _log(err)
        print(f"lceda-kicad-plugin: {err}", file=sys.stderr)
        return 1

    try:
        from lceda_plugin.main import main as app_main
        return app_main()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:  # 启动崩溃兜底:不留死寂,落日志并通知
        import traceback

        _log("主程序启动异常:\n" + traceback.format_exc())
        _notify(f"插件启动失败: {exc}", windows_modal=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
