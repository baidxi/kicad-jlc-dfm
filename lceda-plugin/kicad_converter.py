"""立创标准指令流 → KiCad 原生格式转换器。

语义基准:
- 单位:封装 mil ×0.0254 mm(Y 同向);符号 10mil ×0.254 mm(Y 取反)
  —— 与 KiCad 官方解析器一致(pcb: MilsToIU / sch: 10mil 基准 + Y 镜像)
- 层号映射、THT/SMD 推断、FILL 双形态 path 处理沿用 KicadConverter.cs 结构,
  但该 C# 实现未经端到端测试,几何以官方导入器输出对拍为准(见 plans 阶段 0)
- 输出:封装 → .kicad_mod 文本;符号 → .kicad_sym 文本(含 Supplier 预置属性)
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

_MIL_TO_MM = 0.0254
_SYM_UNIT_TO_MM = 0.254

_LAYER_MAP = {
    1: "F.Cu", 2: "B.Cu",
    3: "F.SilkS", 4: "B.SilkS",
    5: "F.Mask", 6: "B.Mask",
    7: "F.Paste", 8: "B.Paste",
    9: "F.Fab", 10: "B.Fab",
    11: "Edge.Cuts",
    13: "User.Drawings",
    48: "F.Fab", 49: "F.Fab", 50: "F.Fab",
}

# 引脚电气类型文本 → KiCad 电气类型(与官方 sch 解析器思路一致:名称推断兜底)
_PIN_TYPE_MAP = [
    (("INPUT", "IN"), "input"),
    (("OUTPUT", "OUT"), "output"),
    (("BIDIR", "IO"), "bidirectional"),
    (("POWER",), "power_in"),
    (("OPEN COLLECTOR", "OC"), "open_collector"),
    (("OPEN EMITTER", "OE"), "open_emitter"),
    (("3STATE", "TRI", "TS"), "tri_state"),
    (("CLOCK", "CLK"), "clock"),
]


# ── 基础工具 ──

def parse_instructions(doc: str) -> Iterator[list[Any]]:
    """标准格式文档 → 指令列表(每行一条 ["TYPE", ...])。"""
    for raw in doc.splitlines():
        line = raw.strip()
        if not line.startswith("["):
            continue
        try:
            arr = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(arr, list) and arr:
            yield arr


def _s(v: Any) -> str:
    return v if isinstance(v, str) else json.dumps(v)


def _n(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt(v: float) -> str:
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("-0", "") else "0"


def _esc(s: str | None) -> str:
    if not s:
        return '""'
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


def _one_line(s: str | None) -> str:
    return (s or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _pin_elec_type(text: str | None) -> str:
    t = (text or "Undefined").strip().upper()
    for keys, ktype in _PIN_TYPE_MAP:
        if any(k in t for k in keys):
            return ktype
    return "passive"


# ── 封装 ──

def _pad_shape(shape_json: Any) -> tuple[str, float, float]:
    if not isinstance(shape_json, list) or len(shape_json) < 2:
        return "rect", 0.0, 0.0
    stype = _s(shape_json[0])
    a = _n(shape_json[1]) * _MIL_TO_MM
    b = _n(shape_json[2]) * _MIL_TO_MM if len(shape_json) > 2 else a
    if stype == "RECT":
        return "rect", a, b
    if stype in ("OVAL", "ELLIPSE"):
        return ("circle", a, a) if abs(a - b) < 1e-4 else ("oval", a, b)
    if stype == "CIRCLE":
        return "circle", a, a
    return "rect", a, b


def _hole_drill(hole_json: Any) -> str:
    if not isinstance(hole_json, list) or len(hole_json) < 2:
        return " (drill 1)"
    w = abs(_n(hole_json[1])) * _MIL_TO_MM
    h = abs(_n(hole_json[2])) * _MIL_TO_MM if len(hole_json) > 2 else w
    if abs(w - h) < 1e-4:
        return f" (drill {_fmt(w)})"
    return f" (drill (offset 0 0) (oval {_fmt(w)} {_fmt(h)}))"


def _flat_points(pts: Any) -> tuple[list[float], list[float]]:
    """扁平点集 [x1,y1,"L",x2,y2,...] → (xs, ys)。"""
    if not isinstance(pts, list):
        return [], []
    xs: list[float] = []
    ys: list[float] = []
    i = 0
    while i + 1 < len(pts):
        if isinstance(pts[i], (int, float)) and isinstance(pts[i + 1], (int, float)):
            xs.append(_n(pts[i]))
            ys.append(_n(pts[i + 1]))
            i += 2
        else:
            i += 1
    return xs, ys


def _append_segments(body: list[str], pts: Any, layer: str, width: float) -> None:
    xs, ys = _flat_points(pts)
    for i in range(len(xs) - 1):
        body.append(
            f'  (fp_line (start {_fmt(xs[i] * _MIL_TO_MM)} {_fmt(ys[i] * _MIL_TO_MM)}) '
            f'(end {_fmt(xs[i + 1] * _MIL_TO_MM)} {_fmt(ys[i + 1] * _MIL_TO_MM)}) '
            f'(stroke (width {_fmt(width)}) (type solid)) (layer "{layer}"))\n')


def _append_fill_poly(body: list[str], pts: Any, layer: str) -> None:
    xs, ys = _flat_points(pts)
    if len(xs) < 3:
        return
    pts_txt = " ".join(f"(xy {_fmt(x * _MIL_TO_MM)} {_fmt(y * _MIL_TO_MM)})"
                       for x, y in zip(xs, ys))
    body.append(
        f'  (fp_poly (pts {pts_txt}) (stroke (width 0.05) (type solid)) '
        f'(fill solid) (layer "{layer}"))\n')


def _append_fills(body: list[str], path: Any, layer: str) -> None:
    if not isinstance(path, list) or not path:
        return
    if isinstance(path[0], (int, float)):        # 扁平点集:整体一个多边形
        _append_fill_poly(body, path, layer)
        return
    for shape in path:
        if not isinstance(shape, list) or not shape:
            continue
        head = _s(shape[0])
        if head == "CIRCLE" and len(shape) >= 3:
            cx, cy = _n(shape[1]) * _MIL_TO_MM, _n(shape[2]) * _MIL_TO_MM
            r = _n(shape[3]) * _MIL_TO_MM if len(shape) > 3 else 0.5
            body.append(
                f'  (fp_circle (center {_fmt(cx)} {_fmt(cy)}) (end {_fmt(cx + r)} {_fmt(cy)}) '
                f'(stroke (width 0.05) (type solid)) (fill solid) (layer "{layer}"))\n')
        elif isinstance(shape[0], (int, float)) and len(shape) >= 6:
            _append_fill_poly(body, shape, layer)


def convert_footprint(doc: str, name: str, description: str | None = None,
                      designator: str | None = None,
                      model_ref: str | None = None) -> str:
    """封装文档 → .kicad_mod 文本。model_ref 非 None 时注入 (model) 子句。"""
    body: list[str] = []
    ref_text = designator
    value_text: str | None = None
    has_thru = False

    for a in parse_instructions(doc):
        op = _s(a[0])
        if op == "PAD" and len(a) > 14:
            layer_flag = _s(a[4])
            number = _s(a[5])
            x, y = _n(a[6]) * _MIL_TO_MM, _n(a[7]) * _MIL_TO_MM
            rot = _n(a[14])
            hole, shape = a[9], a[10]

            tht = isinstance(hole, list) and len(hole) > 0
            if layer_flag in ("12", "3"):
                tht = True
            has_thru |= tht

            k_shape, w, h = _pad_shape(shape)
            if w <= 0:
                w = h = 1.6

            if tht:
                k_type, layers = "thru_hole", '"*.Cu" "*.Mask"'
                if k_shape == "rect":
                    k_shape = "oval"
            elif layer_flag == "2":
                k_type, layers = "smd", '"B.Cu" "B.Paste" "B.Mask"'
            else:
                k_type, layers = "smd", '"F.Cu" "F.Paste" "F.Mask"'

            rot_part = f" {_fmt(rot)}" if abs(rot) > 0.001 else ""
            drill = _hole_drill(hole) if tht else ""
            body.append(
                f"  (pad {_esc(number)} {k_type} {k_shape} "
                f"(at {_fmt(x)} {_fmt(y)}{rot_part}) "
                f"(size {_fmt(abs(w))} {_fmt(abs(h))}){drill} "
                f"(layers {layers}))\n")
        elif op == "POLY" and len(a) > 6:
            layer = _LAYER_MAP.get(int(_n(a[4])), "F.SilkS")
            width = max(_n(a[5]) * _MIL_TO_MM, 0.05)
            _append_segments(body, a[6], layer, width)
        elif op == "FILL" and len(a) > 7:
            layer_id = int(_n(a[4]))
            if layer_id == 49:
                continue  # 3D 标记层
            k_layer = {5: "F.Mask", 6: "B.Mask", 7: "F.Paste", 8: "B.Paste",
                       9: "F.Fab", 10: "B.Fab", 11: "Edge.Cuts",
                       13: "User.Drawings"}.get(layer_id, "F.Fab")
            _append_fills(body, a[7], k_layer)
        elif op == "ATTR" and len(a) > 8:
            key, val = _s(a[7]), _s(a[8])
            if key == "Designator":
                ref_text = val
            elif key == "Footprint":
                value_text = val

    head: list[str] = []
    head.append(f'(footprint {_esc(name)} (layer "F.Cu")\n')
    if description:
        head.append(f"  (descr {_esc(_one_line(description))})\n")
    head.append(f"  (attr {'through_hole' if has_thru else 'smd'})\n")
    head.append(
        f"  (fp_text reference {_esc(ref_text or 'REF**')} (at 0 -2.4 unlocked) "
        f'(layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))\n')
    head.append(
        f"  (fp_text value {_esc(value_text or name)} (at 0 2.4 unlocked) "
        f'(layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))\n')
    if model_ref:
        head.append(
            f'  (model {_esc(model_ref)}\n    (offset (xyz 0 0 0))\n'
            f"    (scale (xyz 1 1 1))\n    (rotate (xyz 0 0 0))\n  )\n")

    return "".join(head) + "".join(body) + ")\n"


def inject_model(fp_text: str, model_ref: str) -> str:
    """向 .kicad_mod 文本注入/替换 (model) 子句(STEP 后到场景)。"""
    model_block = (
        f'  (model {_esc(model_ref)}\n    (offset (xyz 0 0 0))\n'
        f"    (scale (xyz 1 1 1))\n    (rotate (xyz 0 0 0))\n  )\n")
    # 移除既有 model 子句(简单行扫描,遇到非 model 行保留)
    lines = fp_text.splitlines(keepends=True)
    kept: list[str] = []
    in_model = False
    for ln in lines:
        if re.match(r"\s*\(model\b", ln):
            in_model = True
            continue
        if in_model:
            if re.match(r"\s*\)\s*$", ln):
                in_model = False
            continue
        kept.append(ln)
    text = "".join(kept).rstrip()
    if text.endswith(")"):
        text = text[:-1].rstrip() + "\n"
    return text + model_block + ")\n"


# ── 符号 ──

def convert_symbol(doc: str, name: str, value: str | None = None,
                   designator: str | None = None,
                   footprint: str | None = None,
                   supplier: str | None = None,
                   supplier_part: str | None = None) -> str:
    """符号文档 → .kicad_sym 文本;footprint/supplier/supplier_part 非空时库级预置属性。

    footprint 预置(如 "LCSC:SOT-23-3")使交互式放置出来的元件自动带正确封装引用。
    """
    graphics: list[str] = []
    pins: list[str] = []
    pin_names: dict[str, str] = {}
    pin_numbers: dict[str, str] = {}
    pin_types: dict[str, str] = {}

    for a in parse_instructions(doc):
        op = _s(a[0])
        if op == "RECT" and len(a) > 5:
            x1, y1 = _n(a[2]) * _SYM_UNIT_TO_MM, -_n(a[3]) * _SYM_UNIT_TO_MM
            x2, y2 = _n(a[4]) * _SYM_UNIT_TO_MM, -_n(a[5]) * _SYM_UNIT_TO_MM
            graphics.append(
                f"    (rectangle (start {_fmt(x1)} {_fmt(y1)}) (end {_fmt(x2)} {_fmt(y2)}) "
                f"(stroke (width 0.254) (type default)) (fill (type none)))\n")
        elif op == "CIRCLE" and len(a) > 4:
            cx, cy = _n(a[2]) * _SYM_UNIT_TO_MM, -_n(a[3]) * _SYM_UNIT_TO_MM
            r = _n(a[4]) * _SYM_UNIT_TO_MM
            graphics.append(
                f"    (circle (center {_fmt(cx)} {_fmt(cy)}) (end {_fmt(cx + r)} {_fmt(cy)}) "
                f"(stroke (width 0.254) (type default)) (fill (type none)))\n")
        elif op == "POLY":
            pts = a[2] if len(a) > 2 else None
            if not isinstance(pts, list):
                continue
            filled = (len(a) >= 6 and abs(_n(a[-1]) - 1) < 0.001)
            xs, ys = [], []
            i = 0
            while i + 1 < len(pts):
                if isinstance(pts[i], (int, float)) and isinstance(pts[i + 1], (int, float)):
                    xs.append(_n(pts[i]) * _SYM_UNIT_TO_MM)
                    ys.append(-_n(pts[i + 1]) * _SYM_UNIT_TO_MM)
                    i += 2
                else:
                    i += 1
            if len(xs) < 2:
                continue
            pts_txt = " ".join(f"(xy {_fmt(x)} {_fmt(y)})" for x, y in zip(xs, ys))
            graphics.append(
                f"    (polyline (pts {pts_txt}) (stroke (width 0.254) (type default)) "
                f'(fill (type {"background" if filled else "none"})))\n')
        elif op == "PIN" and len(a) > 7:
            pid = _s(a[1])
            x, y = _n(a[4]) * _SYM_UNIT_TO_MM, -_n(a[5]) * _SYM_UNIT_TO_MM
            length = _n(a[6]) * _SYM_UNIT_TO_MM
            rot = _n(a[7])
            etype = _pin_elec_type(pin_types.get(pid))
            pname = pin_names.get(pid, "")
            pnum = pin_numbers.get(pid, "")
            pins.append(
                f"    (pin {etype} line (at {_fmt(x)} {_fmt(y)} {_fmt(rot)}) "
                f"(length {_fmt(length)}) (name {_esc(pname or '~')}) "
                f"(number {_esc(pnum or '~')}))\n")
        elif op == "ATTR" and len(a) > 4:
            parent, key, val = _s(a[2]), _s(a[3]), _s(a[4])
            if not parent:
                continue  # 全局属性由入参决定
            if key == "NAME":
                pin_names[parent] = val
            elif key == "NUMBER":
                pin_numbers[parent] = val
            elif key == "Pin Type":
                pin_types[parent] = val

    props: list[str] = []
    props.append(f'\t\t(property "Reference" {_esc(designator or "U")}'
                 f' (at 0 3.81 0) (effects (font (size 1.27 1.27))))\n')
    props.append(f'\t\t(property "Value" {_esc(value or name)}'
                 f' (at 0 -3.81 0) (effects (font (size 1.27 1.27))))\n')
    props.append(f'\t\t(property "Footprint" {_esc(footprint or "")}'
                 ' (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))\n')
    props.append('\t\t(property "Datasheet" "" (at 0 0 0)'
                 ' (effects (font (size 1.27 1.27)) (hide yes)))\n')
    props.append('\t\t(property "Description" "" (at 0 0 0)'
                 ' (effects (font (size 1.27 1.27)) (hide yes)))\n')
    if supplier:
        props.append(f'\t\t(property "Supplier" {_esc(supplier)}'
                     ' (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))\n')
    if supplier_part:
        props.append(f'\t\t(property "Supplier Part" {_esc(supplier_part)}'
                     ' (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))\n')

    out: list[str] = []
    out.append("(kicad_symbol_lib\n")
    out.append("\t(version 20241209)\n")
    out.append('\t(generator "lceda_kicad_plugin")\n')
    out.append(f"\t(symbol {_esc(name)}\n")
    out.append("\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n")
    out.extend(props)
    out.append(f"\t\t(symbol {_esc(name + '_0_1')}\n")
    out.extend(graphics)
    out.extend(pins)
    out.append("\t\t)\n\t)\n)\n")
    return "".join(out)


def extract_symbol_block(lib_text: str) -> str:
    """从完整 .kicad_sym 文本中提取首个 (symbol ...) 块(供追加进共享库)。

    匹配 "(symbol "(带空格)恰好不会命中 "(kicad_symbol_lib"(其后是下划线)。
    """
    start = lib_text.find("(symbol ")
    if start < 0:
        raise ValueError("文本中不含 symbol 块")
    depth = 0
    i = start
    in_str = False
    while i < len(lib_text):
        c = lib_text[i]
        if c == '"' and (i == 0 or lib_text[i - 1] != "\\"):
            in_str = not in_str
        elif not in_str:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return lib_text[start:i + 1]
        i += 1
    raise ValueError("symbol 块括号不平衡")
