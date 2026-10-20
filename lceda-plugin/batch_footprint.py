"""批量修改封装(规划 §3.2 三模式)。

A 在线按 Supplier Part:有编号的元件 → 编号定位元件 → 下载对应封装/3D 入库 → 改字段
B 手动指定(离线):统一/逐行指定目标封装,仅改字段
C 通用件手动搜索替换:原理图选中元件 + GUI 内选定的立创元件 → 入库 + 改字段 + 添加属性
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .kicad_ipc import IpcUnavailable, KicadBridge, SymbolInfo
from .lceda_api import LcedaClient, LcedaComponent
from .pipeline import Pipeline
from .settings import Settings


@dataclass
class BatchReport:
    success: list[tuple[str, str]] = field(default_factory=list)   # (reference, 新 Footprint)
    failed: list[tuple[str, str]] = field(default_factory=list)    # (reference, 原因)
    skipped: list[tuple[str, str]] = field(default_factory=list)   # (reference, 原因)

    def summary(self) -> str:
        return (f"成功 {len(self.success)} / 失败 {len(self.failed)} / "
                f"跳过 {len(self.skipped)}")


class BatchUpdater:
    def __init__(self, settings: Settings, client: LcedaClient,
                 pipeline: Pipeline, bridge: KicadBridge) -> None:
        self.settings = settings
        self.client = client
        self.pipeline = pipeline
        self.bridge = bridge

    # ── 模式 A:在线按 Supplier Part ──

    def update_online(self, symbols: list[SymbolInfo],
                      progress=None) -> BatchReport:
        """progress: callable(reference: str, step: str) 用于 GUI 反馈。"""
        report = BatchReport()
        prepared: dict[str, str] = {}          # internal_id → 新 Footprint 值
        add_fields: dict[str, str] = {"Supplier": "LCSC"}
        part_map: dict[str, str] = {}          # internal_id → 编号(报告用)

        for sym in symbols:
            part = (sym.supplier_part or "").strip()
            if not part:
                report.skipped.append((sym.reference, "无 Supplier Part 字段"))
                continue
            try:
                comp = self.client.search_by_part_number(part)
                if comp is None:
                    report.skipped.append((sym.reference, f"编号 {part} 未搜到元件"))
                    continue
                if progress:
                    progress(sym.reference, f"下载 {part} 封装/3D")
                fp_name = self.pipeline.ingest_footprint(comp)
                self.pipeline.ingest_step(comp)
                part_map[sym.internal_id] = comp.clean_part_number() or part
                prepared[sym.internal_id] = f"{self.settings.library_nickname}:{fp_name}"
            except Exception as ex:  # noqa: BLE001 - 单个失败不阻塞批次
                report.failed.append((sym.reference, str(ex)))

        if not prepared:
            return report

        # 编号以清洗后的为准回写(统一一次 commit)
        for sym in symbols:
            sid = sym.internal_id
            if sid in prepared:
                pn = part_map.get(sid)
                if pn:
                    add_fields.setdefault("Supplier Part", pn)  # 同批次同编号场景
        try:
            n = self.bridge.update_footprints(prepared, also_add_fields=add_fields)
            for sym in symbols:
                if sym.internal_id in prepared:
                    report.success.append((sym.reference, prepared[sym.internal_id]))
            if n != len(prepared):
                report.failed.append(("?", f"提交数 {n} != 计划数 {len(prepared)}"))
        except IpcUnavailable as ex:
            for sid, fp in prepared.items():
                ref = next((s.reference for s in symbols if s.internal_id == sid), sid)
                report.failed.append((ref, f"IPC 提交失败: {ex}"))
        return report

    # ── 模式 B:手动指定(离线) ──

    def update_manual(self, assignments: dict[str, str]) -> BatchReport:
        """assignments: {internal_id: "库昵称:封装名"}"""
        report = BatchReport()
        try:
            self.bridge.update_footprints(assignments)
            for sid, fp in assignments.items():
                report.success.append((sid, fp))
        except IpcUnavailable as ex:
            for sid, fp in assignments.items():
                report.failed.append((sid, f"IPC 提交失败: {ex}"))
        return report

    # ── 模式 C:通用件手动搜索替换 ──

    def bind_component(self, symbols: list[SymbolInfo],
                       comp: LcedaComponent, progress=None) -> BatchReport:
        """把一个立创元件的封装/3D 绑定到若干原理图元件(含添加属性)。"""
        report = BatchReport()
        try:
            if progress:
                progress(comp.title, "下载封装/3D")
            fp_name = self.pipeline.ingest_footprint(comp)
            self.pipeline.ingest_step(comp)
            target_fp = f"{self.settings.library_nickname}:{fp_name}"
            prepared = {s.internal_id: target_fp for s in symbols}
            add_fields = {"Supplier": "LCSC"}
            pn = comp.clean_part_number()
            if pn:
                add_fields["Supplier Part"] = pn
            self.bridge.update_footprints(prepared, also_add_fields=add_fields)
            for s in symbols:
                report.success.append((s.reference, target_fp))
        except Exception as ex:  # noqa: BLE001
            for s in symbols:
                report.failed.append((s.reference, str(ex)))
        return report
