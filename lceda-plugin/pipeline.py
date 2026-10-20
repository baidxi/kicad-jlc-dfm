"""放置流水线编排(规划 §3.1):下载 → 转换 → 入库 → 3D 关联。

同步段:符号下载入库(秒级,供 IPC 放置)。
后台段:封装 + STEP 下载入库;无论封装与 STEP 谁先到达,最终 .kicad_mod 都含 (model) 子句。
uuid 去重:同封装/模型只下载一次(多元件共用封装场景)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .lceda_api import LcedaClient, LcedaComponent, get_safe_filename
from . import kicad_converter as conv
from . import lib_manager
from .settings import Settings


@dataclass
class IngestResult:
    symbol_name: str | None = None
    footprint_name: str | None = None
    footprint_file: Path | None = None
    step_file: Path | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class Pipeline:
    def __init__(self, settings: Settings, client: LcedaClient | None = None) -> None:
        self.settings = settings
        self.client = client or LcedaClient()
        # uuid → 名称缓存(进程内去重)
        self._fp_names: dict[str, str] = {}
        self._step_names: dict[str, str] = {}

    # ── 命名 ──

    def symbol_name(self, comp: LcedaComponent) -> str:
        return get_safe_filename(comp.title)

    def footprint_name(self, comp: LcedaComponent) -> str:
        if comp.footprint_uuid and comp.footprint_uuid in self._fp_names:
            return self._fp_names[comp.footprint_uuid]
        return get_safe_filename(comp.footprint_title or comp.title)

    # ── 同步段:符号 ──

    def ingest_symbol(self, comp: LcedaComponent,
                      footprint_ref: str | None = None) -> str:
        """下载转换符号并入库;返回符号名。供放置前同步调用。

        footprint_ref:预置到库符号 Footprint 属性的引用(如 "LCSC:SOT-23-3"),
        交互式放置时字段随库符号带入,无需放置后回写。
        """
        if not comp.symbol_uuid:
            raise RuntimeError(f"元件 {comp.title} 缺少符号 uuid")
        doc, _ = self.client.fetch_document(comp.symbol_uuid)
        name = self.symbol_name(comp)
        text = conv.convert_symbol(
            doc, name, value=comp.title, footprint=footprint_ref,
            supplier="LCSC" if comp.part_number else None,
            supplier_part=comp.part_number)
        lib_manager.add_symbol(self.settings, name, conv.extract_symbol_block(text))
        return name

    # ── 后台段:封装 + 3D ──

    def ingest_footprint(self, comp: LcedaComponent) -> str:
        """下载转换封装并入库;返回封装名。STEP 可能尚未到达(model 后补)。"""
        if not comp.footprint_uuid:
            raise RuntimeError(f"元件 {comp.title} 缺少封装 uuid")

        fp_name = self.footprint_name(comp)
        self._fp_names[comp.footprint_uuid] = fp_name

        doc, detail = self.client.fetch_document(comp.footprint_uuid)
        model_ref = None
        base = self._model_base_for(comp)
        for ext in (".step", ".wrl"):
            candidate = self.settings.model3d_path / (base + ext)
            if candidate.exists():                       # 模型先到:生成时直接带上
                model_ref = self.settings.model_ref_for(candidate.name)
                break

        text = conv.convert_footprint(doc, fp_name,
                                      description=comp.description or detail.get("description"),
                                      model_ref=model_ref)
        lib_manager.add_footprint(self.settings, fp_name, text)
        return fp_name

    def ingest_step(self, comp: LcedaComponent) -> Path | None:
        """下载 3D 模型(STEP/WRL 按内容嗅探)并回写封装的 (model) 子句;无 3D 返回 None。"""
        m3d = comp.model3d_uuid
        if not m3d:
            return None

        # 缓存命中:同 uuid 已下载过,只回写引用
        cached = self._step_names.get(m3d)
        if cached is None:
            base = self._model_base_for(comp)
            cached = self.client.download_model(m3d, self.settings.model3d_path, base)
            self._step_names[m3d] = cached

        # 回写 (model):封装已入库则直接重写文件
        fp_name = self.footprint_name(comp)
        fp_file = lib_manager.footprint_file(self.settings, fp_name)
        if fp_file.exists():
            text = fp_file.read_text(encoding="utf-8")
            updated = conv.inject_model(text, self.settings.model_ref_for(cached.name))
            lib_manager.add_footprint(self.settings, fp_name, updated)
        return cached

    def _model_base_for(self, comp: LcedaComponent) -> str:
        """模型文件基名跟封装名走(多元件共用封装 → 共用模型文件,保证去重)。"""
        if comp.footprint_uuid and comp.footprint_uuid in self._fp_names:
            return self._fp_names[comp.footprint_uuid]
        return get_safe_filename(comp.footprint_title or comp.title)

    # ── 完整入库 ──

    def ingest_all(self, comp: LcedaComponent, with_model: bool = True) -> IngestResult:
        """符号+封装+3D 全量入库(不涉及 IPC 放置)。部分失败保留成功项。"""
        result = IngestResult()
        try:
            result.symbol_name = self.ingest_symbol(comp)
        except Exception as ex:  # noqa: BLE001 - 逐项容错,失败进报告
            result.errors.append(f"符号: {ex}")
        try:
            result.footprint_name = self.ingest_footprint(comp)
            result.footprint_file = lib_manager.footprint_file(
                self.settings, result.footprint_name)
        except Exception as ex:  # noqa: BLE001
            result.errors.append(f"封装: {ex}")
        if with_model:
            try:
                result.step_file = self.ingest_step(comp)
            except Exception as ex:  # noqa: BLE001
                result.errors.append(f"3D 模型: {ex}")
        return result
