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

    # ── 封装决策:内置优先 ──

    def _resolve(self, comp: LcedaComponent,
                 progress=None, ref: str = "", strategy: str | None = None) -> str:
        """封装决策(按策略):返回目标 Footprint 值("lib:name")。

        strategy: builtin_first(默认)| lcsc_first | builtin_only
        """
        strategy = strategy or self.settings.auto_match_strategy or "builtin_first"

        if strategy != "lcsc_first" \
                and not self.pipeline.ensure_builtin_set(self.bridge):
            if progress:
                progress(ref or comp.title,
                         "内置封装列表不可用(KiCad 忙或未连接),本次按立创处理")

        if strategy in ("builtin_first", "builtin_only"):
            builtin_id, _src = self.pipeline.resolve_footprint(comp)
            if builtin_id:
                if progress:
                    progress(ref or comp.title, f"命中 KiCad 内置封装 {builtin_id}")
                return builtin_id
            if strategy == "builtin_only":
                raise ValueError("无 KiCad 内置封装匹配(仅内置策略)")

        if progress:
            progress(ref or comp.title, "下载立创封装/3D")
        fp_name = self.pipeline.ingest_footprint(comp)
        self.pipeline.ingest_step(comp)
        return f"{self.settings.library_nickname}:{fp_name}"

    # ── 智能自动匹配 ──

    def _best_by_value(self, value: str, current_fp: str) -> LcedaComponent | None:
        """按元件值(如 100nF / AO3400 / 10k)搜索立创并挑最佳候选。

        评分:编号精确 > 标题含关键词 > 封装尺寸码与现封装一致(保留原尺寸偏好)。
        """
        from . import builtin_map  # noqa: PLC0415

        kw = (value or "").strip()
        if not kw:
            return None
        results = self.client.smart_search(kw)
        full = [c for c in results if c.symbol_uuid and c.footprint_uuid]
        if not full:
            return None
        code = builtin_map._find_code((current_fp or "").upper())  # noqa: SLF001

        def score(c: LcedaComponent) -> int:
            s = 0
            t = (c.title or "").lower()
            pn = (c.part_number or "").lower()
            if kw.lower() == pn:
                s += 8
            elif kw.lower() in t:
                s += 4
            if code and code in (c.footprint_title or "").upper():
                s += 2
            return s

        full.sort(key=score, reverse=True)
        return full[0]

    def auto_match(self, symbols: list[SymbolInfo], strategy: str | None = None,
                   progress=None) -> BatchReport:
        """自动匹配封装(批量页「自动匹配」):

        - 有 Supplier Part:按编号精确定位立创元件
        - 无编号:按元件值搜索立创,智能挑最佳(编号/标题/封装尺寸码评分)
        - 封装按策略决定(内置优先/立创优先/仅内置);无论内置或立创,
          都回写 Supplier=LCSC 与 Supplier Part 编号
        """
        report = BatchReport()
        prepared: dict[str, str] = {}
        per_fields: dict[str, dict[str, str]] = {}
        # 批量搜索缓存:同编号/同值+同封装码的元件(如几十个 100nF)只搜一次
        cache: dict[str, LcedaComponent | None] = {}

        for sym in symbols:
            try:
                comp: LcedaComponent | None = None
                part = (sym.supplier_part or "").strip()
                key = f"p:{part}" if part else \
                    f"v:{sym.value}:{(sym.footprint or '')[-12:]}"
                if key in cache:
                    comp = cache[key]
                elif part:
                    comp = self.client.search_by_part_number(part)
                    if comp is None and progress:
                        progress(sym.reference, f"编号 {part} 未搜到,改按值搜索")
                    cache[key] = comp
                if comp is None and key not in cache:
                    comp = self._best_by_value(sym.value, sym.footprint)
                    cache[key] = comp
                if comp is None:
                    report.skipped.append((sym.reference, "编号与值均未匹配到元件"))
                    continue

                target = self._resolve(comp, progress=progress,
                                       ref=sym.reference, strategy=strategy)
                prepared[sym.internal_id] = target
                fields = {"Supplier": "LCSC"}
                pn = comp.clean_part_number()
                if pn:
                    fields["Supplier Part"] = pn
                per_fields[sym.internal_id] = fields
            except Exception as ex:  # noqa: BLE001 - 单个失败不阻塞批次
                report.failed.append((sym.reference, str(ex)))

        if not prepared:
            return report
        try:
            n = self.bridge.update_footprints(prepared, per_symbol_fields=per_fields)
            for sym in symbols:
                if sym.internal_id in prepared:
                    report.success.append((sym.reference, prepared[sym.internal_id]))
            if n != len(prepared):
                report.failed.append(("?", f"提交数 {n} != 计划数 {len(prepared)}"))
        except Exception as ex:  # noqa: BLE001
            for sym in symbols:
                if sym.internal_id in prepared:
                    report.failed.append((sym.reference, f"IPC 提交失败: {ex}"))
        return report

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
                target = self._resolve(comp, progress=progress, ref=sym.reference)
                part_map[sym.internal_id] = comp.clean_part_number() or part
                prepared[sym.internal_id] = target
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
            target_fp = self._resolve(comp, progress=progress)
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
