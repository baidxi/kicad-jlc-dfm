"""lceda-kicad-plugin GUI(PySide6)。

两个功能页:
- 放置:搜索立创元件 → 选中放置到原理图(符号同步,封装/3D 后台入库关联)
- 批量改封装:A 在线按 Supplier Part 更新 / B 手动指定 / C 用放置页选中元件绑定

所有网络/IPC 操作在 QThread 中执行,UI 不阻塞;日志区输出流水线状态。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Any

# 优先使用工作区 kicad-python(含 interactive 扩展),先于 site-packages 旧版
_APP_ROOT = Path(__file__).resolve().parent.parent
_KICAD_PYTHON = _APP_ROOT / "kicad-python"
if _KICAD_PYTHON.is_dir():
    sys.path.insert(0, str(_KICAD_PYTHON))

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QHeaderView, QLabel, QLineEdit, QListWidget,
    QMainWindow, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
    QTextEdit, QVBoxLayout, QHBoxLayout, QWidget, QMessageBox,
)

from .batch_footprint import BatchReport, BatchUpdater
from .kicad_ipc import IpcUnavailable, KicadBridge, SymbolInfo
from .lceda_api import LcedaClient, LcedaComponent
from .pipeline import Pipeline
from .settings import Settings


class PreviewPopup(QWidget):
    """非模态元件预览小窗(Tool 窗口):单击搜索结果行时显示/更新。"""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool)
        self.setWindowTitle("元件预览")
        self.resize(380, 320)
        self._title = QLabel("")
        self._title.setWordWrap(True)
        self._img = QLabel("无预览图")
        self._img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img.setFixedSize(360, 240)     # 固定尺寸,确保简介始终排在图片下方
        self._img.setStyleSheet("background: #f5f5f5;")
        self._desc = QLabel("")
        self._desc.setWordWrap(True)
        self._desc.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay = QVBoxLayout(self)
        lay.addWidget(self._title)
        lay.addWidget(self._img)
        lay.addWidget(self._desc)

    def show_comp(self, title: str, pixmap: QPixmap | None,
                  description: str = "") -> None:
        self._title.setText(title)
        self._desc.setText(description.strip())
        self._desc.setVisible(bool(description.strip()))
        if pixmap is not None:
            self._img.setPixmap(pixmap.scaled(
                360, 240,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self._img.clear()
            self._img.setText("该元件无预览图")
        self.show()
        self.raise_()


def _fmt_err(ex: Exception) -> str:
    return f"{ex.__class__.__name__}: {ex}"


class ReplaceSearchDialog(QDialog):
    """行内「搜索替换」对话框:在线搜索立创元件,选中后返回给调用方绑定。"""

    def __init__(self, client: LcedaClient, reference: str, current_value: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"搜索替换 — {reference}({current_value})")
        self.resize(860, 520)
        self.client = client
        self.selected: LcedaComponent | None = None

        v = QVBoxLayout(self)
        row = QHBoxLayout()
        self._box = QLineEdit()
        self._box.setPlaceholderText("输入关键词或立创编号,回车搜索")
        self._box.returnPressed.connect(self.on_search)
        btn = QPushButton("搜索")
        btn.clicked.connect(self.on_search)
        row.addWidget(self._box, 1)
        row.addWidget(btn)
        v.addLayout(row)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["元件", "编号", "封装"])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.doubleClicked.connect(lambda _: self.accept_selection())
        self._table.itemSelectionChanged.connect(self._show_pv)

        # 右侧预览面板:图片 + 简介
        self._pv_img = QLabel("单击左侧元件查看预览")
        self._pv_img.setFixedSize(240, 180)
        self._pv_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pv_img.setStyleSheet("background: #f5f5f5;")
        self._pv_desc = QLabel("")
        self._pv_desc.setWordWrap(True)
        self._pv_desc.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._pv_desc.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        pv_box = QVBoxLayout()
        pv_box.addWidget(self._pv_img)
        pv_box.addWidget(self._pv_desc, 1)
        pv_panel = QWidget()
        pv_panel.setLayout(pv_box)
        pv_panel.setMinimumWidth(250)

        body = QHBoxLayout()
        body.addWidget(self._table, 3)
        body.addWidget(pv_panel, 1)
        v.addLayout(body, 1)

        bottom = QHBoxLayout()
        tip = QLabel("选中一行后点确认;封装与 3D 会自动下载入库并替换 Footprint")
        tip.setWordWrap(True)
        bottom.addWidget(tip, 1)
        ok = QPushButton("替换所选元件")
        ok.setEnabled(False)
        ok.clicked.connect(self.accept_selection)
        bottom.addWidget(ok)
        v.addLayout(bottom)
        self._ok = ok
        self._ok.setEnabled(False)

    def _show_pv(self) -> None:
        rows = self._table.selectedIndexes()
        self._ok.setEnabled(bool(rows))
        if not rows:
            return
        comp: LcedaComponent | None = self._table.item(rows[0].row(), 0).data(
            Qt.ItemDataRole.UserRole)
        if comp is None:
            return
        pixmap = None
        if comp.images:
            try:
                data = self.client.fetch_image(comp.images[0])
                pixmap = QPixmap()
                pixmap.loadFromData(data)
            except Exception as ex:  # noqa: BLE001
                self._pv_desc.setText(f"预览图加载失败: {ex}")
        if pixmap is not None:
            self._pv_img.setPixmap(pixmap.scaled(
                240, 180,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self._pv_img.clear()
            self._pv_img.setText("无预览图")
        self._pv_desc.setText((comp.description or "").strip())

    def on_search(self) -> None:
        kw = self._box.text().strip()
        if not kw:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            results = self.client.smart_search(kw)
        except Exception as ex:  # noqa: BLE001
            results = []
            QMessageBox.warning(self, "搜索失败", _fmt_err(ex))
        finally:
            QApplication.restoreOverrideCursor()

        self._table.setRowCount(0)
        for c in results:
            if not (c.symbol_uuid and c.footprint_uuid):
                continue  # 缺符号/封装的条目无法完成替换
            r = self._table.rowCount()
            self._table.insertRow(r)
            for col, val in enumerate([c.title, c.part_number or "—",
                                       c.footprint_title or "—"]):
                item = QTableWidgetItem(val)
                item.setData(Qt.ItemDataRole.UserRole, c)
                self._table.setItem(r, col, item)
        if not results:
            QMessageBox.information(self, "无结果", f"未搜到「{kw}」")

    def accept_selection(self) -> None:
        rows = self._table.selectedIndexes()
        if not rows:
            return
        self.selected: LcedaComponent | None = self._table.item(
            rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        self.accept()


# ── 后台任务 ──

class FootprintPickerDialog(QDialog):
    """模式 B 选择器:列出 KiCad 全部已注册封装(内置 + 用户库),支持实时过滤。"""

    def __init__(self, items: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择封装(全部已注册封装库)")
        self.resize(560, 560)
        self.selected: str | None = None

        v = QVBoxLayout(self)
        self._box = QLineEdit()
        self._box.setPlaceholderText("过滤:如 0603 / SOT-23 / LCSC(实时,大小写不敏感)")
        self._box.setClearButtonEnabled(True)
        self._box.textChanged.connect(self._apply_filter)
        v.addWidget(self._box)

        self._list = QListWidget()
        self._list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self._list.addItems(items)
        self._list.doubleClicked.connect(lambda _: self.accept_selection())
        v.addWidget(self._list, 1)

        self._count = QLabel(f"共 {len(items)} 个封装")
        v.addWidget(self._count)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = QPushButton("应用到选中元件")
        ok.setEnabled(False)
        ok.clicked.connect(self.accept_selection)
        row.addWidget(ok)
        v.addLayout(row)
        self._ok = ok
        self._list.itemSelectionChanged.connect(
            lambda: self._ok.setEnabled(bool(self._list.selectedIndexes())))

    def _apply_filter(self) -> None:
        kw = self._box.text().strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            item.setHidden(bool(kw) and kw not in item.text().lower())

    def accept_selection(self) -> None:
        sel = self._list.selectedItems()
        if sel:
            self.selected = sel[0].text()
            self.accept()


class SearchWorker(QThread):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, client: LcedaClient, keyword: str) -> None:
        super().__init__()
        self.client, self.keyword = client, keyword

    def run(self) -> None:
        try:
            self.done.emit(self.client.smart_search(self.keyword))
        except Exception as ex:  # noqa: BLE001
            self.failed.emit(_fmt_err(ex))


class PlaceWorker(QThread):
    """放置流水线:符号同步入库+放置,封装/3D 同线程随后补全(不阻塞 UI)。

    交互模式:开始时请求最小化插件窗口;放置后自动选中该符号(高亮),
    用户按 M 即可像原生操作一样移动到目标位置。
    """

    log = Signal(str)
    placed = Signal(str)
    finished_ok = Signal()
    failed = Signal(str)
    minimize_requested = Signal()

    def __init__(self, pipeline: Pipeline, bridge: KicadBridge,
                 comp: LcedaComponent, position_mm: tuple[float, float]) -> None:
        super().__init__()
        self.pipeline, self.bridge, self.comp = pipeline, bridge, comp
        self.position_mm = position_mm

    def run(self) -> None:
        comp = self.comp
        try:
            self.minimize_requested.emit()
            self.log.emit(f"[1/4] 下载符号 {comp.title}")
            fp_name = self.pipeline.footprint_name(comp)
            fp_ref = f"{self.pipeline.settings.library_nickname}:{fp_name}"
            # 字段(Footprint/Supplier/Supplier Part/Value)全部预置于库符号,
            # 交互式放置落位后元件自动携带
            symbol_name = self.pipeline.ingest_symbol(comp, footprint_ref=fp_ref)

            self.log.emit(f"[2/4] 交互放置 {fp_ref}(元件跟随鼠标,左键落位,"
                          "可连续点击多放,Esc 结束)")
            self.bridge.place_symbol(symbol_name, {}, interactive=True)
            self.placed.emit(comp.title)

            self.log.emit(f"[3/4] 后台:封装 {fp_name}")
            self.pipeline.ingest_footprint(comp)

            self.log.emit("[4/4] 后台:3D 模型")
            step = self.pipeline.ingest_step(comp)
            self.log.emit(f"完成(3D: {step.name if step else '元件未绘制模型'})")
            self.log.emit("从任务栏恢复本窗口可继续放置下一个元件")
            self.finished_ok.emit()
        except Exception as ex:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(_fmt_err(ex))


class ListSymbolsWorker(QThread):
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, bridge: KicadBridge) -> None:
        super().__init__()
        self.bridge = bridge

    def run(self) -> None:
        try:
            self.done.emit(list(self.bridge.list_symbols()))
        except Exception as ex:  # noqa: BLE001
            self.failed.emit(_fmt_err(ex))


class BatchWorker(QThread):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str, str)

    def __init__(self, updater: BatchUpdater, mode: str, symbols: list[SymbolInfo],
                 comp: LcedaComponent | None = None,
                 assignments: dict[str, str] | None = None) -> None:
        super().__init__()
        self.updater, self.mode = updater, mode
        self.symbols, self.comp, self.assignments = symbols, comp, assignments

    def run(self) -> None:
        try:
            if self.mode == "online":
                self.done.emit(self.updater.update_online(
                    self.symbols,
                    progress=lambda ref, step: self.progress.emit(ref, step)))
            elif self.mode == "bind" and self.comp is not None:
                self.done.emit(self.updater.bind_component(
                    self.symbols, self.comp,
                    progress=lambda ref, step: self.progress.emit(ref, step)))
            elif self.mode == "manual" and self.assignments is not None:
                self.done.emit(self.updater.update_manual(self.assignments))
        except Exception as ex:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(_fmt_err(ex))


# ── 主窗口 ──

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        import time  # noqa: PLC0415
        self.setWindowTitle(f"立创元件 → KiCad (IPC)  ·  {time.strftime('%H:%M:%S')} 启动")
        self.resize(1000, 660)

        self.settings = Settings.load()
        self.client = LcedaClient()
        self.pipeline = Pipeline(self.settings, self.client)
        self.bridge = KicadBridge(self.settings)
        self.updater = BatchUpdater(self.settings, self.client,
                                    self.pipeline, self.bridge)

        self._ipc_ok = self.bridge.connect()
        self._selected_comp: LcedaComponent | None = None
        self._batch_symbols: list[SymbolInfo] = []
        self._fp_cache: list[str] | None = None
        self._workers: list[QThread] = []
        self._place_count = 0

        tabs = QTabWidget()
        tabs.addTab(self._build_place_tab(), "搜索放置")
        tabs.addTab(self._build_batch_tab(), "批量改封装")
        self.setCentralWidget(tabs)

    # ── Tab 1:搜索放置 ──

    def _build_place_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        ipc_row = QHBoxLayout()
        self._ipc_label = QLabel(
            "已连接 KiCad 原理图" if self._ipc_ok
            else "未连接 KiCad(仅下载入库,无法放置/批量改)")
        self._ipc_label.setStyleSheet(
            "color: green" if self._ipc_ok else "color: orange")
        ipc_row.addWidget(self._ipc_label)
        ipc_row.addStretch()
        v.addLayout(ipc_row)

        row = QHBoxLayout()
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("输入关键词或立创编号,如 AO3400 / C20917 / 0603 100nF")
        self._search_box.returnPressed.connect(self.on_search)
        btn = QPushButton("搜索")
        btn.clicked.connect(self.on_search)
        row.addWidget(self._search_box, 1)
        row.addWidget(btn)
        v.addLayout(row)

        self._result_table = QTableWidget(0, 4)
        self._result_table.setHorizontalHeaderLabels(
            ["元件", "编号", "封装", "3D"])
        # 元件/编号/封装按内容宽度;3D 列弹性加宽(吸收剩余空间)
        header = self._result_table.horizontalHeader()
        for col in (0, 1, 2):
            header.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setStretchLastSection(False)
        self._result_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._result_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection)
        self._result_table.itemSelectionChanged.connect(self.on_result_selected)

        # 预览面板:嵌入页内右侧,单击行就地更新(独立窗口在 Wayland 下无法定位);
        # 面板底部为「放置到原理图」按钮
        self._pv_title = QLabel("单击左侧元件查看预览")
        self._pv_title.setWordWrap(True)
        self._pv_img = QLabel()
        self._pv_img.setFixedSize(300, 220)
        self._pv_img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pv_img.setStyleSheet("background: #f5f5f5;")
        self._pv_desc = QLabel("")
        self._pv_desc.setWordWrap(True)
        self._pv_desc.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._pv_desc.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._pv_place_btn = QPushButton("放置到原理图")
        self._pv_place_btn.setEnabled(False)
        self._pv_place_btn.clicked.connect(self.on_preview_place)
        pv_box = QVBoxLayout()
        pv_box.addWidget(self._pv_title)
        pv_box.addWidget(self._pv_img)
        pv_box.addWidget(self._pv_desc, 1)
        pv_box.addWidget(self._pv_place_btn)
        pv_panel = QWidget()
        pv_panel.setLayout(pv_box)
        pv_panel.setMinimumWidth(310)
        pv_panel.setMaximumWidth(360)

        body = QHBoxLayout()
        body.addWidget(self._result_table, 3)
        body.addWidget(pv_panel, 1)
        v.addLayout(body, 2)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(140)
        v.addWidget(self._log, 1)
        self._log.append("在 KiCad 原理图打开的状态下启动本工具可启用放置功能。")
        return w

    # ── Tab 2:批量改封装 ──

    def _build_batch_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        row = QHBoxLayout()
        refresh = QPushButton("读取原理图元件")
        refresh.clicked.connect(self.on_refresh_symbols)
        row.addWidget(refresh)
        self._filter_box = QLineEdit()
        self._filter_box.setPlaceholderText("过滤:输入参考/值/编号关键字(实时)")
        self._filter_box.setClearButtonEnabled(True)
        self._filter_box.textChanged.connect(lambda _: self._render_symbols())
        row.addWidget(self._filter_box, 1)
        v.addLayout(row)

        self._sym_table = QTableWidget(0, 5)
        self._sym_table.setHorizontalHeaderLabels(
            ["参考", "值", "现 Footprint", "Supplier Part", "替换"])
        # 值列按内容宽度(修复过宽),Footprint 列弹性填充
        header = self._sym_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self._sym_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._sym_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        # 单击行 → KiCad 原理图同步高亮 + 视图聚焦该元件
        self._sym_table.itemSelectionChanged.connect(self.on_sym_selection_changed)
        v.addWidget(self._sym_table, 2)

        btns = QHBoxLayout()
        b_online = QPushButton("A: 在线按 Supplier Part 更新选中")
        b_online.clicked.connect(self.on_batch_online)
        b_manual = QPushButton("B: 手动指定封装…")
        b_manual.clicked.connect(self.on_batch_manual)
        b_bind = QPushButton("C: 绑定为放置页选中元件")
        b_bind.clicked.connect(self.on_batch_bind)
        for b in (b_online, b_manual, b_bind):
            btns.addWidget(b)
        v.addLayout(btns)

        self._batch_log = QTextEdit()
        self._batch_log.setReadOnly(True)
        v.addWidget(self._batch_log, 1)
        return w

    # ── 工具 ──

    def _track(self, worker: QThread) -> None:
        self._workers.append(worker)
        worker.finished.connect(lambda: self._workers.remove(worker)
                                if worker in self._workers else None)

    def _log(self, msg: str) -> None:
        self._log.append(msg)

    def _selected_symbol_infos(self) -> list[SymbolInfo]:
        rows = sorted({i.row() for i in self._sym_table.selectedIndexes()})
        out = []
        for r in rows:
            info: Any = self._sym_table.item(r, 0).data(Qt.ItemDataRole.UserRole)
            if info is not None:
                out.append(info)
        return out

    # ── Tab1 槽 ──

    def on_search(self) -> None:
        kw = self._search_box.text().strip()
        if not kw:
            return
        self._log.append(f"搜索:{kw}")
        self._worker = SearchWorker(self.client, kw)
        self._worker.done.connect(self._show_results)
        self._worker.failed.connect(lambda m: self._log.append(f"[搜索失败] {m}"))
        self._track(self._worker)
        self._worker.start()

    def _show_results(self, comps: list[LcedaComponent]) -> None:
        self._result_table.setRowCount(0)
        for c in comps:
            r = self._result_table.rowCount()
            self._result_table.insertRow(r)
            vals = [c.title, c.part_number or "—",
                    c.footprint_title or "—", "✓" if c.model3d_uuid else "✗"]
            for col, val in enumerate(vals):
                item = QTableWidgetItem(val)
                item.setData(Qt.ItemDataRole.UserRole, c)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if col == 3:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self._result_table.setItem(r, col, item)
        self._log.append(f"结果 {len(comps)} 条")

    def on_result_selected(self) -> None:
        rows = self._result_table.selectedIndexes()
        if not rows:
            return
        self._selected_comp = self._result_table.item(rows[0].row(), 0).data(
            Qt.ItemDataRole.UserRole)
        if self._selected_comp is not None:
            c = self._selected_comp
            self._log.append(f"[选中] {c.title}({c.part_number}) — "
                             f"点右侧「放置到原理图」即可放置")
            self._pv_place_btn.setEnabled(True)
            self._show_preview(c)

    def _show_preview(self, comp: LcedaComponent) -> None:
        """单击结果行,更新页内右侧预览面板(图片 + 简介)。"""
        self._pv_title.setText(f"{comp.title}   {comp.part_number or ''}")
        pixmap = None
        if comp.images:
            try:
                data = self.client.fetch_image(comp.images[0])
                pixmap = QPixmap()
                pixmap.loadFromData(data)
            except Exception as ex:  # noqa: BLE001 - 图片失败不影响选择
                self._log.append(f"[预览图加载失败] {_fmt_err(ex)}")
        if pixmap is not None:
            self._pv_img.setPixmap(pixmap.scaled(
                self._pv_img.width(), self._pv_img.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self._pv_img.clear()
            self._pv_img.setText("无预览图")
        self._pv_desc.setText(comp.description.replace("\r\n", "\n").strip())

    def on_preview_place(self) -> None:
        """预览面板底部的放置按钮:放置当前预览的元件。"""
        if self._selected_comp is None:
            QMessageBox.information(self, "提示", "请先在搜索结果中单击一个元件")
            return
        self._start_place(self._selected_comp)

    def _start_place(self, comp: LcedaComponent) -> None:
        # 连接可能已失效(KiCad 重启/重开图纸):每次放置前重检,失败重连一次
        if not self.bridge.connected:
            self._ipc_ok = self.bridge.connect()
            self._ipc_label.setText(
                "已连接 KiCad 原理图" if self._ipc_ok
                else "未连接 KiCad(仅下载入库,无法放置/批量改)")
            self._ipc_label.setStyleSheet(
                "color: green" if self._ipc_ok else "color: orange")
        if not self._ipc_ok:
            QMessageBox.warning(self, "未连接 KiCad",
                                "无法连接 KiCad IPC API。\n"
                                "请确认 KiCad 已启动且原理图已打开,"
                                "然后关闭本窗口并从工具栏图标重新打开插件。")
            return
        # 连续放置:网格错开避免重叠;落位后符号自动选中,按 M 微调到目标位置
        pos = (120.0 + (self._place_count % 5) * 25.4,
               60.0 + (self._place_count // 5) * 25.4)
        self._place_count += 1
        w = PlaceWorker(self.pipeline, self.bridge, comp, pos)
        w.log.connect(self._log.append)   # 信号必须连接到方法,不能直接连接控件
        w.minimize_requested.connect(self.showMinimized)
        w.placed.connect(lambda m: self._log.append(f"[已放置] {m}"))
        w.failed.connect(self._on_place_failed)
        self._track(w)
        w.start()

    def _on_place_failed(self, msg: str) -> None:
        self._log.append(f"[放置失败] {msg}")
        QMessageBox.warning(self, "放置失败", msg)

    # ── Tab2 槽 ──

    def on_sym_selection_changed(self) -> None:
        """批量表格单击行(单选)→ KiCad 原理图同步高亮 + 视图聚焦该元件。"""
        infos = self._selected_symbol_infos()
        if len(infos) != 1:
            return
        try:
            self.bridge.select_symbol(infos[0].internal_id)
            self.bridge.focus_symbol(infos[0].internal_id)
        except Exception:  # noqa: BLE001 - 联动失败静默
            pass

    def on_refresh_symbols(self) -> None:
        w = ListSymbolsWorker(self.bridge)
        w.done.connect(self._show_symbols)
        w.failed.connect(lambda m: self._batch_log.append(f"[读取失败] {m}"))
        self._track(w)
        w.start()

    def _show_symbols(self, symbols: list[SymbolInfo]) -> None:
        self._batch_symbols = list(symbols)
        self._render_symbols()
        self._batch_log.append(f"读取到 {len(symbols)} 个元件")

    def _render_symbols(self) -> None:
        """按过滤框渲染表格(只影响显示;批量操作仍以全量数据+选中行为准)。"""
        kw = self._filter_box.text().strip().lower() if hasattr(self, "_filter_box") else ""
        self._sym_table.setRowCount(0)
        shown = skipped = 0
        for s in self._batch_symbols:
            if s.reference.startswith("#"):
                skipped += 1     # 电源标志等内部符号(#PWR?),不参与封装替换
                continue
            if kw and kw not in s.reference.lower() \
                    and kw not in s.value.lower() \
                    and kw not in s.supplier_part.lower() \
                    and kw not in s.footprint.lower():
                continue
            r = self._sym_table.rowCount()
            self._sym_table.insertRow(r)
            for col, val in enumerate([s.reference, s.value, s.footprint or "—",
                                       s.supplier_part or "—"]):
                item = QTableWidgetItem(val)
                item.setData(Qt.ItemDataRole.UserRole, s)
                self._sym_table.setItem(r, col, item)
            btn = QPushButton("搜索替换")
            btn.clicked.connect(lambda _, info=s: self.on_row_replace(info))
            self._sym_table.setCellWidget(r, 4, btn)
            shown += 1
        if skipped:
            self._batch_log.append(f"已隐藏 {skipped} 个电源/网标;当前显示 {shown} 个")

    def on_row_replace(self, info: SymbolInfo) -> None:
        """行内「搜索替换」:在线搜索立创元件并绑定到该元件(模式 C 单行版)。"""
        dlg = ReplaceSearchDialog(self.client, info.reference, info.value, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.selected is None:
            return
        symbols = [s for s in self._batch_symbols
                   if s.internal_id == info.internal_id]
        if not symbols:
            return
        w = BatchWorker(self.updater, "bind", symbols, comp=dlg.selected)
        w.progress.connect(lambda r, step: self._batch_log.append(f"[{r}] {step}"))
        w.done.connect(lambda r: self._on_replace_done(r, info.internal_id))
        w.failed.connect(lambda m: self._batch_log.append(f"[替换失败] {m}"))
        self._track(w)
        w.start()

    def _on_replace_done(self, report, internal_id: str) -> None:
        self._show_batch_report(report)
        # 无论成败都强制重读:成功→反映新字段;失败→反映真实状态(过滤条件保留)
        self.on_refresh_symbols()
        # 视图聚焦到被替换的元件(高亮 + 居中),便于确认结果
        try:
            self.bridge.select_symbol(internal_id)
            self.bridge.focus_symbol(internal_id)
        except Exception:  # noqa: BLE001
            pass

    def _show_batch_report(self, report: BatchReport) -> None:
        for ref, fp in report.success:
            self._batch_log.append(f"[成功] {ref} → {fp}")
        for ref, why in report.failed:
            self._batch_log.append(f"[失败] {ref}: {why}")
        for ref, why in report.skipped:
            self._batch_log.append(f"[跳过] {ref}: {why}")
        self._batch_log.append(report.summary())

    def on_batch_online(self) -> None:
        symbols = self._selected_symbol_infos()
        if not symbols:
            QMessageBox.information(self, "提示", "请先读取并勾选要更新的元件行")
            return
        w = BatchWorker(self.updater, "online", symbols)
        w.progress.connect(lambda r, s: self._batch_log.append(f"[{r}] {s}"))
        w.done.connect(self._show_batch_report)
        w.failed.connect(lambda m: self._batch_log.append(f"[批量失败] {m}"))
        self._track(w)
        w.start()

    def on_batch_manual(self) -> None:
        """模式 B:从全部已注册封装库(KiCad 内置 + 用户库)中选择封装。"""
        symbols = self._selected_symbol_infos()
        if not symbols:
            QMessageBox.information(self, "提示", "请先读取并勾选元件行")
            return

        if self._fp_cache is None:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self._fp_cache = self.bridge.list_all_footprints()
            except Exception as ex:  # noqa: BLE001
                QApplication.restoreOverrideCursor()
                QMessageBox.warning(self, "获取封装列表失败", _fmt_err(ex))
                return
            QApplication.restoreOverrideCursor()

        dlg = FootprintPickerDialog(self._fp_cache, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.selected:
            return
        assignments = {s.internal_id: dlg.selected for s in symbols}
        w = BatchWorker(self.updater, "manual", symbols, assignments=assignments)
        w.done.connect(self._show_batch_report)
        w.failed.connect(lambda m: self._batch_log.append(f"[批量失败] {m}"))
        self._track(w)
        w.start()

    def on_batch_bind(self) -> None:
        symbols = self._selected_symbol_infos()
        if not symbols:
            QMessageBox.information(self, "提示", "请先读取并勾选元件行")
            return
        if self._selected_comp is None:
            QMessageBox.information(
                self, "提示", "请先到「搜索放置」页搜索并选中一个立创元件")
            return
        w = BatchWorker(self.updater, "bind", symbols, comp=self._selected_comp)
        w.progress.connect(lambda r, s: self._batch_log.append(f"[{r}] {s}"))
        w.done.connect(self._show_batch_report)
        w.failed.connect(lambda m: self._batch_log.append(f"[绑定失败] {m}"))
        self._track(w)
        w.start()

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        self.client.close()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
