"""插件设置:KiCad 目录探测 + 持久化。

探测规则(优先级从高到低):
1. 用户显式配置(settings.json)
2. ~/.config/kicad/ 与 ~/.local/share/kicad/ 下版本号最大的目录
   (如 10.99 优先于 10.0;排序按 (主版本, 次版本) 数值比较)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

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


def detect_config_dir() -> Path | None:
    """探测 KiCad 用户配置目录(~/.config/kicad/<版本>)。"""
    for p in _sorted_kicad_versions(Path.home() / ".config" / "kicad"):
        return p
    return None


def detect_data_dir() -> Path | None:
    """探测 KiCad 用户数据目录(~/.local/share/kicad/<版本>)。"""
    for p in _sorted_kicad_versions(Path.home() / ".local" / "share" / "kicad"):
        return p
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
        return detect_data_dir() or (Path.home() / ".local" / "share" / "kicad" / "10.99")

    @property
    def config_dir(self) -> Path:
        return detect_config_dir() or (Path.home() / ".config" / "kicad" / "10.99")

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
        """生成 .kicad_mod 中 (model) 子句使用的路径字符串。"""
        if self.model3d_use_variable:
            version = self.data_dir.name
            return "${KICAD%s_3DMODEL_DIR}/%s" % (version.replace(".", ""), step_filename)
        return str(self.model3d_path / step_filename)

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
