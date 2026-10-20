"""KiCad 全局库管理:.kicad_sym/.pretty 写入 + 库表幂等注册。

IPC API 没有库内容创建能力(见规划 §3.3),库构建只能走文件:
- 符号库:单文件 <昵称>.kicad_sym,多 symbol 追加/替换(s-表达式块平衡匹配)
- 封装库:<昵称>.pretty/ 目录,一封装一文件,天然隔离
- 库表:sym-lib-table / fp-lib-table 追加 (lib (name ...) ...) 条目,按 name 幂等
并发保护:跨进程文件锁(flock),防止放置流水线与批量更新同时写库。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import tempfile
from pathlib import Path

from .settings import Settings

_LOCK_DIR = Path.home() / ".cache" / "lceda-kicad-plugin"


@contextlib.contextmanager
def _file_lock(name: str):
    """进程间互斥锁(库文件与库表的关键区)。"""
    _LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _LOCK_DIR / f"{name}.lock"
    with open(lock_path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)  # 原子替换,避免半写文件被 KiCad 读到
    finally:
        with contextlib.suppress(OSError):
            if os.path.exists(tmp):
                os.unlink(tmp)


# ── 库表 ──

def _lib_entry(nickname: str, uri: str, descr: str) -> str:
    return (f'\t(lib (name "{nickname}") (type "KiCad") (uri "{uri}") '
            f'(options "") (descr "{descr}"))')


def register_in_lib_table(table_path: Path, nickname: str, uri: str,
                          descr: str = "") -> bool:
    """向库表幂等注册条目;返回是否发生修改。表不存在时创建空表。"""
    with _file_lock("lib_table"):
        if table_path.exists():
            text = table_path.read_text(encoding="utf-8")
        else:
            decl = "sym_lib_table" if table_path.name == "sym-lib-table" else "fp_lib_table"
            text = f"({decl}\n\t(version 7)\n)\n"

        if re.search(r'\(name\s+"' + re.escape(nickname) + r'"\s*\)', text):
            return False  # 已注册,幂等

        closing = text.rfind(")")
        if closing < 0:
            raise ValueError(f"库表格式异常: {table_path}")
        text = text[:closing] + _lib_entry(nickname, uri, descr) + "\n" + text[closing:]
        _atomic_write(table_path, text)
        return True


def unregister_from_lib_table(table_path: Path, nickname: str) -> bool:
    """从库表移除条目(设置页改昵称时用);返回是否发生修改。"""
    with _file_lock("lib_table"):
        if not table_path.exists():
            return False
        text = table_path.read_text(encoding="utf-8")
        pattern = re.compile(
            r"^\t\(lib \(name \"" + re.escape(nickname) + r"\"[^)]*\)\)\n",
            re.MULTILINE)
        new_text, n = pattern.subn("", text)
        if n:
            _atomic_write(table_path, new_text)
        return bool(n)


# ── 符号库(单文件多 symbol) ──

_LIB_HEADER_RE = re.compile(r"^\s*\(version\s+\d+\)", re.MULTILINE)


def ensure_symbol_lib(settings: Settings) -> Path:
    """确保符号库文件存在且库表已注册;返回库文件路径。"""
    lib = settings.sym_lib_file
    if not lib.exists():
        _atomic_write(lib, "(kicad_symbol_lib\n\t(version 20241209)\n"
                           '\t(generator "lceda_kicad_plugin")\n)\n')
    register_in_lib_table(
        settings.sym_lib_table, settings.library_nickname, str(lib),
        descr="LCSC components (lceda-kicad-plugin)")
    return lib


def _remove_symbol_block(lib_text: str, name: str) -> str:
    """从库文本中删除同名 symbol 块(括号平衡扫描)。"""
    start = lib_text.find(f'(symbol "{name}"')
    if start < 0:
        return lib_text
    depth = 0
    i = start
    in_str = False
    while i < len(lib_text):
        c = lib_text[i]
        if c == '"' and lib_text[i - 1] != "\\":
            in_str = not in_str
        elif not in_str:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
        i += 1
    return lib_text[:start] + lib_text[i:]


def add_symbol(settings: Settings, name: str, symbol_text: str) -> None:
    """向符号库追加/替换一个 symbol(symbol_text 为单个 (symbol ...) 块)。"""
    ensure_symbol_lib(settings)
    with _file_lock("symbol_lib"):
        lib_text = settings.sym_lib_file.read_text(encoding="utf-8")
        lib_text = _remove_symbol_block(lib_text, name)
        block = symbol_text.rstrip()
        if not block.startswith("(symbol "):
            raise ValueError("symbol_text 必须是单个 (symbol ...) 块")
        closing = lib_text.rfind(")")
        if closing < 0:
            raise ValueError(f"符号库格式异常: {settings.sym_lib_file}")
        lib_text = lib_text[:closing] + block + "\n" + lib_text[closing:]
        _atomic_write(settings.sym_lib_file, lib_text)


# ── 封装库(一封装一文件) ──

def ensure_footprint_lib(settings: Settings) -> Path:
    """确保 .pretty 目录存在且库表已注册;返回目录路径。"""
    lib_dir = settings.fp_lib_dir
    lib_dir.mkdir(parents=True, exist_ok=True)
    register_in_lib_table(
        settings.fp_lib_table, settings.library_nickname, str(lib_dir),
        descr="LCSC components (lceda-kicad-plugin)")
    return lib_dir


def add_footprint(settings: Settings, name: str, fp_text: str) -> Path:
    """写入单个封装文件(覆盖语义:先删后写,Unix O_EXCL 兜底)。"""
    lib_dir = ensure_footprint_lib(settings)
    target = lib_dir / f"{name}.kicad_mod"
    with _file_lock("footprint_lib"):
        if target.exists():
            target.unlink()
        _atomic_write(target, fp_text)
    return target


def footprint_file(settings: Settings, name: str) -> Path:
    return settings.fp_lib_dir / f"{name}.kicad_mod"
