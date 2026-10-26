r"""插件设置:KiCad 目录探测 + 持久化。

KiCad 各平台目录约定(paths.cpp):
| 用途                 | Linux                    | Windows                    | macOS                          |
|----------------------|--------------------------|----------------------------|--------------------------------|
| 设置(全局库表)      | ~/.config/kicad/<ver>    | %APPDATA%\kicad\<ver>      | ~/Library/Preferences/kicad    |
| 用户数据(用户库)    | ~/.local/share/kicad     | Documents\KiCad\<ver>      | ~/Documents/KiCad/<ver>        |
| 缓存(python venv)   | ~/.cache/kicad/<ver>     | %LOCALAPPDATA%\KiCad\<ver> | ~/Library/Caches/kicad/<ver>   |

探测规则(优先级从高到低):
1. 用户显式配置(settings.json)
2. 平台对应基目录下版本号最大的目录(如 10.99 优先于 10.0)
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

if os.name == "nt":
    SETTINGS_DIR = Path.home() / "AppData" / "Roaming" / "lceda-kicad-plugin"
else:
    SETTINGS_DIR = Path.home() / ".config" / "lceda-kicad-plugin"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")


def _sorted_kicad_versions(base: Path) -> list[Path]:
    """返回 base 下按版本号降序排列的 KiCad 版本目录。"""
    if not base.is_dir():
        return []
    entries = []
    for child in base.iterdir():
        m = _VERSION_RE.match(child.name)
        if m and child.is_dir():
            entries.append(((int(m.group(1)), int(m.group(2))), child))
    entries.sort(key=lambda e: e[0], reverse=True)
    return [p for _, p in entries]


def _settings_base() -> Path:
    """KiCad 设置基目录(仅由 OS 类型 + 标准相对路径推导,不依赖环境变量)。"""
    if os.name == "nt":
        return Path.home() / "AppData" / "Roaming" / "kicad"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / "kicad"
    return Path.home() / ".config" / "kicad"


def _data_bases() -> list[Path]:
    """KiCad 用户数据基目录候选(按优先级,同样不依赖环境变量)。"""
    if os.name == "nt":
        return [Path.home() / "Documents" / "KiCad",
                Path.home() / "AppData" / "Roaming" / "kicad",
                Path.home() / "AppData" / "Local" / "KiCad"]
    if sys.platform == "darwin":
        return [Path.home() / "Documents" / "KiCad"]
    return [Path.home() / ".local" / "share" / "kicad"]


def _data_dir_from_package() -> Path | None:
    """PCM 安装布局下从本包位置反推数据目录:

    <data>/3rdparty/plugins/<package>/<plugin>/lceda_plugin/settings.py
      parents: [0]=lceda_plugin [1]=<plugin> [2]=<package> [3]=plugins [4]=3rdparty
    """
    here = Path(__file__).resolve().parents
    if len(here) >= 5 and here[3].name == "plugins" and here[4].name == "3rdparty":
        return here[4].parent
    return None


def detect_config_dir() -> Path | None:
    """探测 KiCad 用户设置目录(全局库表 sym-lib-table 所在)。"""
    found = _sorted_kicad_versions(_settings_base())
    return found[0] if found else None


def detect_data_dir() -> Path | None:
    """探测 KiCad 用户数据目录(用户符号/封装/3D 库所在)。"""
    from_pkg = _data_dir_from_package()
    if from_pkg is not None:
        return from_pkg
    for base in _data_bases():
        found = _sorted_kicad_versions(base)
        if found:
            return found[0]
    return None


@dataclass
class Settings:
    """所有路径均可被 settings.json 覆盖;昵称同时用于库表注册与 Footprint 字段前缀。"""

    library_nickname: str = "lcsc"
    # 库文件位置(None = 随 data_dir 默认)
    symbol_lib_file: str | None = None      # <...>/symbols/LCSC.kicad_sym
    footprint_lib_dir: str | None = None    # <...>/footprints/LCSC.pretty
    model3d_dir: str | None = None          # <...>/3dmodels
    # 3D 路径写法:绝对路径兜底 100% 可用;变量形式更可移植但依赖 KiCad 版本变量
    model3d_use_variable: bool = False

    # ── 派生路径 ──

    @property
    def data_dir(self) -> Path:
        if os.name == "nt":
            fallback = Path.home() / "Documents" / "KiCad" / "10.99"
        else:
            fallback = Path.home() / ".local" / "share" / "kicad" / "10.99"
        return detect_data_dir() or fallback

    @property
    def config_dir(self) -> Path:
        fallback = _settings_base() / "10.99"
        return detect_config_dir() or fallback

    @property
    def sym_lib_file(self) -> Path:
        if self.symbol_lib_file:
            return Path(self.symbol_lib_file)
        return self.data_dir / "symbols" / f"{self.library_nickname}.kicad_sym"

    @property
    def fp_lib_dir(self) -> Path:
        if self.footprint_lib_dir:
            return Path(self.footprint_lib_dir)
        return self.data_dir / "footprints" / f"{self.library_nickname}.pretty"

    @property
    def model3d_path(self) -> Path:
        if self.model3d_dir:
            return Path(self.model3d_dir)
        return self.data_dir / "3dmodels"

    @property
    def sym_lib_table(self) -> Path:
        return self.config_dir / "sym-lib-table"

    @property
    def fp_lib_table(self) -> Path:
        return self.config_dir / "fp-lib-table"

    def model_ref_for(self, step_filename: str) -> str:
        """生成 .kicad_mod 中 (model) 子句使用的路径字符串(正斜杠,s-expr 友好)。"""
        if self.model3d_use_variable:
            version = self.data_dir.name
            return "${KICAD%s_3DMODEL_DIR}/%s" % (version.replace(".", ""), step_filename)
        return str(self.model3d_path / step_filename).replace("\\", "/")

    # ── 持久化 ──

    def save(self) -> None:
        try:
            SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # 保存失败不影响主流程

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            if SETTINGS_FILE.exists():
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
        except (OSError, json.JSONDecodeError):
            pass  # 损坏时回退默认
        return s
