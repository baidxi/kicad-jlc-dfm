"""KiCad 全局库管理:.kicad_sym/.pretty 写入 + 库表幂等注册。

IPC API 没有库内容创建能力(见规划 §3.3),库构建只能走文件:
- 符号库:单文件 <昵称>.kicad_sym,多 symbol 追加/替换(s-表达式块平衡匹配)
- 封装库:<昵称>.pretty/ 目录,一封装一文件,天然隔离
- 库表:sym-lib-table / fp-lib-table 追加 (lib (name ...) ...) 条目,按 name 幂等
并发保护:跨进程文件锁(flock),防止放置流水线与批量更新同时写库。
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import threading
import time
from pathlib import Path

from .settings import Settings

# 插件自有锁/缓存基目录(Windows 放 %LOCALAPPDATA% 惯例位置,路径由
# OS 类型 + 标准相对路径推导,不依赖环境变量)
if os.name == "nt":
    _LOCK_DIR = Path.home() / "AppData" / "Local" / "lceda-kicad-plugin"
else:
    _LOCK_DIR = Path.home() / ".cache" / "lceda-kicad-plugin"
_BACKUP_DIR = _LOCK_DIR / "backups"

_THR_LOCKS: dict[str, threading.Lock] = {}
_THR_LOCKS_GUARD = threading.Lock()

if os.name == "nt":
    import msvcrt

    @contextlib.contextmanager
    def _file_lock(name: str):
        """Windows:msvcrt 字节范围锁(轮询等待,最长约 30 秒)。"""
        _LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = _LOCK_DIR / f"{name}.lock"
        with open(lock_path, "a+") as fh:
            deadline = time.time() + 30
            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise
                    time.sleep(0.1)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    @contextlib.contextmanager
    def _file_lock(name: str):
        """POSIX:flock 跨进程互斥。"""
        _LOCK_DIR.mkdir(parents=True, exist_ok=True)
        lock_path = _LOCK_DIR / f"{name}.lock"
        with open(lock_path, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _thr_lock(name: str) -> threading.Lock:
    """进程内互斥(flock 之外的双保险,防同进程线程竞态)。"""
    with _THR_LOCKS_GUARD:
        return _THR_LOCKS.setdefault(name, threading.Lock())


def _balanced(text: str) -> bool:
    """s-表达式括号平衡检查(跳过字符串字面量)。"""
    depth = 0
    in_str = False
    for i, c in enumerate(text):
        if c == '"' and (i == 0 or text[i - 1] != "\\"):
            in_str = not in_str
        elif not in_str:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth < 0:
                    return False
    return depth == 0


def _backup(path: Path, keep: int = 5) -> None:
    """写前快照(保留最近 keep 份),供损坏自愈。"""
    if not path.exists():
        return
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = _BACKUP_DIR / f"{path.stem}-{stamp}{path.suffix}"
    try:
        target.write_bytes(path.read_bytes())
    except OSError:
        return
    backups = sorted(_BACKUP_DIR.glob(f"{path.stem}-*{path.suffix}"))
    for old in backups[:-keep]:
        with contextlib.suppress(OSError):
            old.unlink()


def _latest_backup(path: Path) -> Path | None:
    if not _BACKUP_DIR.is_dir():
        return None
    found = sorted(_BACKUP_DIR.glob(f"{path.stem}-*{path.suffix}"))
    return found[-1] if found else None


@contextlib.contextmanager

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
    """向库表幂等注册条目;返回是否发生修改。表不存在时创建空表。

    URI 统一正斜杠:Windows 反斜杠在 s-expr 字符串中虽可解析,
    但正斜杠在 KiCad 全平台(含路径变量替换)下最稳。
    """
    uri = str(uri).replace("\\", "/")
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


def symbol_exists(settings: Settings, name: str) -> bool:
    """本地符号库是否已含指定符号(供放置本地优先判断)。"""
    try:
        return f'(symbol "{name}"' in settings.sym_lib_file.read_text(
            encoding="utf-8")
    except OSError:
        return False


def add_symbol(settings: Settings, name: str, symbol_text: str) -> None:
    """向符号库追加/替换一个 symbol(symbol_text 为单个 (symbol ...) 块)。

    双重互斥(进程内 threading.Lock + 跨进程 flock);写前平衡校验与快照
    备份、写后回读校验 —— 防半写、旧快照覆盖与外部工具写坏,损坏时自动
    从最近备份自愈。
    """
    ensure_symbol_lib(settings)
    lib = settings.sym_lib_file
    with _thr_lock("symbol_lib"), _file_lock("symbol_lib"):
        lib_text = lib.read_text(encoding="utf-8")
        if not _balanced(lib_text):
            bak = _latest_backup(lib)
            if bak is None or not _balanced(bak.read_text(encoding="utf-8")):
                raise RuntimeError(f"符号库括号不平衡且无可用备份: {lib}")
            lib_text = bak.read_text(encoding="utf-8")
            _atomic_write(lib, lib_text)   # 自愈:回到最近完好快照

        _backup(lib)

        lib_text = _remove_symbol_block(lib_text, name)
        block = symbol_text.rstrip()
        if not block.startswith("(symbol "):
            raise ValueError("symbol_text 必须是单个 (symbol ...) 块")
        if not _balanced(block):
            # 旧版转换器/extract 的坏块会多带 lib 闭合括号,全文平衡可能
            # 被后续写入"补偿性"抵消,必须按单块校验
            raise RuntimeError(f"符号块括号不平衡,已拒绝写入: {name}")
        closing = lib_text.rfind(")")
        if closing < 0:
            raise ValueError(f"符号库格式异常: {lib}")
        new_text = lib_text[:closing] + block + "\n" + lib_text[closing:]
        if not _balanced(new_text):
            raise RuntimeError(f"新符号块导致括号不平衡,已拒绝写入: {name}")
        _atomic_write(lib, new_text)

        # 写后回读:确认落盘内容含新符号且平衡(防滞后覆盖/半写)
        check = lib.read_text(encoding="utf-8")
        if not _balanced(check) or f'(symbol "{name}"' not in check:
            raise RuntimeError(
                f"符号库写入校验失败(可能被外部进程覆盖): {lib};"
                f"写前快照保留在 {_BACKUP_DIR}")


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
    if not _balanced(fp_text):
        raise RuntimeError(f"封装文本括号不平衡,已拒绝写入: {name}")
    lib_dir = ensure_footprint_lib(settings)
    target = lib_dir / f"{name}.kicad_mod"
    with _thr_lock("footprint_lib"), _file_lock("footprint_lib"):
        if target.exists():
            target.unlink()
        _atomic_write(target, fp_text)
    return target


def footprint_file(settings: Settings, name: str) -> Path:
    return settings.fp_lib_dir / f"{name}.kicad_mod"
