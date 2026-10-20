#!/usr/bin/env python3
"""lceda-kicad-plugin 程序入口(开发/手工运行)。

源码目录名为 `lceda-plugin`(含连字符,不能被 Python 直接 import),
此处把它注册为虚拟包 `lceda_plugin` 后再启动 GUI。
"""

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_PKG_DIR = _ROOT / "lceda-plugin"

if _PKG_DIR.is_dir() and "lceda_plugin" not in sys.modules:
    pkg = types.ModuleType("lceda_plugin")
    pkg.__path__ = [str(_PKG_DIR)]
    pkg.__version__ = "0.1.0"
    sys.modules["lceda_plugin"] = pkg

# 工作区 kicad-python(interactive 扩展)优先于 site-packages 旧版
_KICAD_PYTHON = _ROOT / "kicad-python"
if _KICAD_PYTHON.is_dir():
    sys.path.insert(0, str(_KICAD_PYTHON))

from lceda_plugin.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
