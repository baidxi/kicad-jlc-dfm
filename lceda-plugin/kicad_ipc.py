"""kipy(IPC API)封装:连接、放置符号、批量改封装字段。

已在 KiCad 10.99 实测(2026-10,联调记录):
- `schematic.get_symbols()` 返回 **SchematicSymbolInstance**:
  `reference`(str)/ `value`(str)/ `footprint_field.text.value` /
  `user_fields`(Sequence[SchematicField],元素 .name + .text.value)/ `id`(KIID)
- 新增自定义字段:`instance.user_fields = list + [SchematicField()]`(构造后赋 name/text.value)
- `reload_library(type, scope, nicknames)`,scope 必须非 LTS_BOTH;运行时入库在全局表 → LTS_GLOBAL
- `place_symbol_from_library(lib_id, Vector2.from_xy_mm(...), ...)` → SchematicSymbolInstance

能力边界(规划 §3.3):放置/改字段/重载库走 IPC;库内容构建走文件(lib_manager)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .settings import Settings


class IpcUnavailable(RuntimeError):
    """KiCad 未运行 / kipy 不可用 / 原理图未打开。"""


class KiCadBusyError(RuntimeError):
    """KiCad 长时间忙(通常是交互放置工具尚未结束)。"""


class LibraryNotLoaded(RuntimeError):
    """库表条目是 KiCad 启动后注册的,KiCad 内存中尚无该库(需重启一次)。"""


def _diag(msg: str) -> None:
    """轻量诊断日志(缓存目录 ipc.log;Windows 位于 %LOCALAPPDATA% 惯例位置)。"""
    try:
        import os  # noqa: PLC0415
        from datetime import datetime  # noqa: PLC0415

        if os.name == "nt":
            d = Path.home() / "AppData" / "Local" / "lceda-kicad-plugin"
        else:
            d = Path.home() / ".cache" / "lceda-kicad-plugin"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "ipc.log", "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


@dataclass
class SymbolInfo:
    """原理图 symbol 的展示快照(供表格)。"""

    internal_id: str          # KIID 字符串,用于回写定位
    reference: str
    value: str
    footprint: str
    supplier_part: str
    lib_id: str = ""


class KicadBridge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._kicad: Any = None
        self._schematic: Any = None

    @staticmethod
    def _retry(fn, attempts: int = 6, delay: float = 0.8):
        """KiCad 忙时(用户正在交互/撤销等)API 返回 busy 错误,等待重试。"""
        import time  # noqa: PLC0415
        for i in range(attempts):
            try:
                return fn()
            except Exception as ex:  # noqa: BLE001
                busy = "busy" in str(ex).lower()
                if not busy or i == attempts - 1:
                    if busy:
                        raise KiCadBusyError(
                            "KiCad 持续忙(重试约 5 秒无果)。常见原因:"
                            "①交互放置模式仍在进行——在 KiCad 中按 Esc 结束;"
                            "②有对话框/编辑工具未关闭。稍候再试。") from ex
                    raise
                time.sleep(delay)

    # ── 连接 ──

    @property
    def connected(self) -> bool:
        return self._schematic is not None

    def connect(self) -> bool:
        """连接运行中的 KiCad 并获取打开的原理图;失败返回 False(降级纯下载模式)。"""
        try:
            import kipy  # noqa: PLC0415

            _diag(f"kipy 运行时路径: {kipy.__file__}")   # 排查混装(venv vs bundled)
            from kipy.kicad import KiCad  # 延迟导入:未装 kipy 时其余功能不受影响
            self._kicad = KiCad()
            self._kicad.get_version()  # 探活
            self._schematic = self._kicad.get_schematic()
            return True
        except Exception:  # noqa: BLE001 - IPC 不可用是常态而非异常路径
            self._kicad = None
            self._schematic = None
            return False

    def _require_schematic(self) -> Any:
        if self._schematic is None and not self.connect():
            raise IpcUnavailable("KiCad 未连接或没有打开的原理图")
        return self._schematic

    # ── 库重载(运行时入库后必须调用) ──

    def reload_symbol_library(self, nickname: str | None = None) -> None:
        assert self._kicad is not None
        from kipy.kicad import LibraryTableScope, LibraryType  # noqa: PLC0415
        # 入库写的是全局库表,scope 用 LTS_GLOBAL(reload 不接受 BOTH)。
        # reload 为异步:调用方需 wait_for_libraries 等待内容可用(place 前)。
        self._retry(lambda: self._kicad.reload_library(
            LibraryType.LT_SYMBOL, LibraryTableScope.LTS_GLOBAL,
            [nickname or self.settings.library_nickname]))
        self._retry(lambda: self._kicad.wait_for_libraries(
            types=LibraryType.LT_SYMBOL, scope=LibraryTableScope.LTS_GLOBAL,
            timeout_s=30))

    def library_has_symbol(self, symbol_name: str) -> bool:
        assert self._kicad is not None
        lib_id = f"{self.settings.library_nickname}:{symbol_name}"
        try:
            self._kicad.get_library_symbol(lib_id)
            return True
        except Exception:  # noqa: BLE001
            return False

    # ── 放置 ──

    def place_symbol(self, symbol_name: str, fields: dict[str, str],
                     position_mm: tuple[float, float] = (0.0, 0.0),
                     interactive: bool = False) -> str:
        """从 LCSC 库放置符号。

        interactive=True:交给 KiCad 交互放置工具(元件跟随鼠标,左键落位)。
        此时 fields 必须已预置于库符号(转换器完成),返回符号名。

        interactive=False:程序化直落 position_mm 并回写字段,返回 reference。
        """
        sch = self._require_schematic()
        lib_id = f"{self.settings.library_nickname}:{symbol_name}"

        try:
            self.reload_symbol_library()   # 内部已 wait_for_libraries
        except Exception as ex:  # noqa: BLE001
            if "not found" in str(ex).lower():
                raise LibraryNotLoaded(
                    f"{self.settings.library_nickname} 库尚未被 KiCad 加载:"
                    "库表是在 KiCad 启动后注册的,请重启 KiCad 后重试;"
                    "此步骤仅需一次。") from ex
            raise

        # 诊断(不拦截):get_library_symbol 只查询 KiCad"已加载"的库,
        # 启动后惰性未加载的库这里查不到,但放置路径仍可能按需解析成功,
        # 以它做硬拦截会造成误报(现象:重启后仍提示未加载)。
        if not self._retry(lambda: self.library_has_symbol(symbol_name)):
            self._diag(f"预检未见 {lib_id}(库可能尚未加载);继续尝试放置")

        from kipy.geometry import Vector2  # noqa: PLC0415

        # 能力检测:interactive 需要客户端签名与 proto 消息字段同时具备。
        # 混装环境(venv 官方 kicad-python 与 bundled fork 并存)下可能出现
        # "签名有 interactive 而 pb2 没有",只查签名会在写 proto 时抛
        # AttributeError。不支持时降级为程序化直落(官方 KiCad 的预期行为)。
        import inspect  # noqa: PLC0415
        supports = "interactive" in inspect.signature(
            sch.place_symbol_from_library).parameters
        if supports:
            try:
                from kipy.proto.schematic import (
                    schematic_commands_pb2,  # noqa: PLC0415
                )
                supports = hasattr(
                    schematic_commands_pb2.PlaceSymbolFromLibrary(),
                    "interactive")
            except Exception:  # noqa: BLE001
                supports = False
        if interactive and not supports:
            interactive = False
            _diag("KiCad/kipy 不支持 interactive 放置,降级为程序化直落")

        def _place() -> Any:
            return sch.place_symbol_from_library(
                lib_id, Vector2.from_xy_mm(position_mm[0], position_mm[1]),
                interactive=interactive)

        try:
            instance = self._retry(_place)
        except Exception as ex:  # noqa: BLE001
            low = str(ex).lower()
            if "not found" in low and symbol_name.lower() in low:
                # 再给一轮机会:库可能在异步加载中,重载后重试一次
                self._diag(f"放置 {lib_id} 报 not found;重试一轮 reload+放置")
                try:
                    self.reload_symbol_library()
                except Exception:  # noqa: BLE001
                    pass
                if self._retry(lambda: self.library_has_symbol(symbol_name)):
                    instance = self._retry(_place)
                else:
                    raise LibraryNotLoaded(
                        f"KiCad 进程尚未加载 {self.settings.library_nickname} "
                        f"库(无法放置 {lib_id})。库表条目晚于 KiCad 启动,"
                        "请重启 KiCad 后重试;仅需一次。") from ex
            else:
                raise

        if interactive:
            return symbol_name
        if instance is None:
            raise RuntimeError(f"放置返回空结果: {lib_id}")

        for name, value in fields.items():
            self._set_field(instance, name, value, add_if_missing=True)
        sch.update_items([instance])
        commit = sch.begin_commit()
        sch.push_commit(commit, "lceda-kicad-plugin: place symbol")
        try:
            sch.clear_selection()
            sch.add_to_selection(instance)   # 高亮,提示用户按 M 移动
        except Exception:  # noqa: BLE001 - 选中失败不影响放置
            pass
        return instance.reference

    # ── 读取与批量修改 ──

    def list_symbols(self) -> list[SymbolInfo]:
        sch = self._require_schematic()
        out: list[SymbolInfo] = []
        for s in self._retry(lambda: sch.get_symbols()):
            spart = self._user_field(s, "Supplier Part")
            out.append(SymbolInfo(
                internal_id=str(s.id),
                reference=s.reference,
                value=s.value,
                footprint=s.footprint_field.text.value,
                supplier_part=spart,
            ))
        return out

    def focus_symbol(self, internal_id: str) -> bool:
        """让 KiCad 视图聚焦(切换 sheet + 缩放居中)到指定元件。"""
        import re  # noqa: PLC0415

        sch = self._require_schematic()
        m = re.search(r"[0-9a-fA-F-]{36}", internal_id)
        if not m:
            return False
        uuid = m.group(0)

        def _do() -> bool:
            from google.protobuf.empty_pb2 import Empty  # noqa: PLC0415
            from kipy.proto.common.commands import (  # noqa: PLC0415
                editor_commands_pb2 as ec,
            )
            cmd = ec.FocusOnItems()
            cmd.document.CopyFrom(sch._doc)          # noqa: SLF001 - kipy 未封装
            item = cmd.items.add()
            item.value = uuid
            self._kicad._client.send(cmd, Empty)     # noqa: SLF001 - FocusOnItems 未封装
            return True

        try:
            return bool(self._retry(_do))
        except Exception:  # noqa: BLE001 - 聚焦失败静默
            return False

    def list_all_footprints(self) -> list[str]:
        """列出全部已注册封装库中的封装(nickname:name,含 KiCad 内置库)。"""
        assert self._kicad is not None
        return [str(x) for x in self._kicad.get_library_footprints()]

    def select_symbol(self, internal_id: str) -> bool:
        """在原理图中选中指定元件(插件表格点击 → KiCad 高亮联动)。

        返回是否成功;KiCad 忙时静默失败(不影响插件操作)。
        """
        sch = self._require_schematic()

        def _do() -> bool:
            target = None
            for s in sch.get_symbols():
                if str(s.id) == internal_id:
                    target = s
                    break
            if target is None:
                return False
            sch.clear_selection()
            sch.add_to_selection(target)
            return True

        try:
            return bool(self._retry(_do))
        except Exception:  # noqa: BLE001 - 联动失败静默
            return False

    def update_footprints(self, updates: dict[str, str],
                          also_add_fields: dict[str, str] | None = None) -> int:
        """按 internal_id(KIID 字符串)批量改 Footprint 字段,单次 commit;返回修改数。

        also_add_fields:对每个被修改的 symbol 写入字段(如 Supplier/Supplier Part),不存在则新增。
        """
        sch = self._require_schematic()
        changed: list[Any] = []
        for s in sch.get_symbols():
            if str(s.id) not in updates:
                continue
            s.footprint_field.text.value = updates[str(s.id)]
            for k, v in (also_add_fields or {}).items():
                self._set_user_field(s, k, v)
            changed.append(s)
        if not changed:
            return 0
        self._retry(lambda: sch.update_items(changed))
        commit = self._retry(sch.begin_commit)
        self._retry(lambda: sch.push_commit(commit, "lceda-kicad-plugin: update footprints"))
        return len(changed)

    # ── 字段工具(全部在 SchematicSymbolInstance 语义上) ──

    @staticmethod
    def _user_field(instance: Any, name: str) -> str:
        for f in instance.user_fields:
            if f.name == name:
                return f.text.value
        return ""

    @staticmethod
    def _set_user_field(instance: Any, name: str, value: str) -> None:
        """写入 user_field:存在则改值,不存在则追加(本地构造 SchematicField)。"""
        for f in instance.user_fields:
            if f.name == name:
                f.text.value = value
                return
        from kipy.schematic_types import SchematicField  # noqa: PLC0415
        nf = SchematicField()
        nf.name = name
        nf.text.value = value
        instance.user_fields = list(instance.user_fields) + [nf]

    @classmethod
    def _set_field(cls, instance: Any, name: str, value: str,
                   add_if_missing: bool = False) -> None:
        """按字段名写值;命名 specialize 字段直接用对应 property。"""
        named = {
            "Reference": lambda: setattr(instance, "reference", value),
            "Value": lambda: setattr(instance, "value", value),
            "Footprint": lambda: setattr(instance.footprint_field.text, "value", value),
            "Datasheet": lambda: setattr(instance.datasheet_field.text, "value", value),
        }
        if name in named:
            named[name]()
            return
        if name in ("Supplier", "Supplier Part"):
            cls._set_user_field(instance, name, value)
            return
        # 其余命名属性兜底:instance 上没有通用 fields 列表,报告明确错误
        raise RuntimeError(f"暂不支持写入字段 {name!r}(仅支持 Reference/Value/"
                           "Footprint/Datasheet/Supplier/Supplier Part)")
