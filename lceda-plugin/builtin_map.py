"""KiCad 内置封装优先匹配(通用件:电阻/电容/电感/三极管/二极管等)。

立创封装名普遍不标准,识别分三层:
  1. 形态段(下划线首段,剥离 "_L2.9-W1.3-P1.90…" 之类尺寸后缀)
       SOT-23-3 / SOT-23 / SOD-323FL / SOT-223 …   → Package_TO_SOT_SMD:*
       R0603 / C0805 / L1210(前缀 + 4 位尺寸码)  → 对应片式库
  2. 无前缀尺寸码 + 类别词:形态段为纯尺寸码("0603")或封装名任意位置含
     尺寸码时,从 元件标题/描述 中的类别词(电阻→R,电容→C,电感→L)补出前缀
  3. 都不中 → 返回 None,调用方回退立创下载(永不阻塞)

返回值只是候选,必须经调用方在"已注册封装全集"中验证存在后方可使用
(如片式电感 L_1210_3225Metric 在官方库不存在时会自动回退立创)。
"""

from __future__ import annotations

import re

# 片式尺寸码 → KiCad Metric 命名(顺序即优先级:长的先匹配避免 0603 命中 603)
_CHIP_CODES = [
    "008004", "0201", "0402", "0603", "0805",
    "1206", "1210", "1806", "1812", "1825", "2010", "2512",
]
_CHIP_METRIC = {
    "008004": "0201Metric",
    "0201": "0603Metric",
    "0402": "1005Metric",
    "0603": "1608Metric",
    "0805": "2012Metric",
    "1206": "3216Metric",
    "1210": "3225Metric",
    "1806": "4516Metric",
    "1812": "4532Metric",
    "1825": "4564Metric",
    "2010": "5025Metric",
    "2512": "6332Metric",
}

# 片式前缀 → KiCad 库
_CHIP_LIB = {"R": "Resistor_SMD", "C": "Capacitor_SMD", "L": "Inductor_SMD"}

# 类别词(中/英,小写子串匹配)→ 片式前缀
_CATEGORY_WORDS = [
    (("电阻", "resistor"), "R"),
    (("电容", "capacitor"), "C"),
    (("电感", "磁珠", "inductor", "ferrite"), "L"),
]

# SOT/SOD 形态 → KiCad 官方 lib_id(长键优先,避免 SOT-23 命中 SOT-23-5)
_TO_SOT = {
    "SOT-23-8": "Package_TO_SOT_SMD:SOT-23-8",
    "SOT-23-6": "Package_TO_SOT_SMD:SOT-23-6",
    "SOT-23-5": "Package_TO_SOT_SMD:SOT-23-5",
    "SOT-23-3": "Package_TO_SOT_SMD:SOT-23",
    "SOT-23":   "Package_TO_SOT_SMD:SOT-23",
    "SOT-223":  "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
    "SOT-89":   "Package_TO_SOT_SMD:SOT-89-3",
    "SOT-143":  "Package_TO_SOT_SMD:SOT-143",
    "SOD-123":  "Package_TO_SOT_SMD:SOD-123",
    "SOD-323":  "Package_TO_SOT_SMD:SOD-323",
    "SOD-523":  "Package_TO_SOT_SMD:SOD-523",
    "SOD-723":  "Package_TO_SOT_SMD:SOD-723",
    "SOD-923":  "Package_TO_SOT_SMD:SOD-923",
}
_TO_SOT_KEYS = sorted(_TO_SOT, key=len, reverse=True)

_CHIP_PREFIX_RE = re.compile(r"^([RCL])(\d{4})")


def _match_sot(head: str) -> str | None:
    """形态段匹配 SOT/SOT-23-x/SOD-x(允许尾随字母,如 SOD-323FL)。"""
    for key in _TO_SOT_KEYS:
        if head.startswith(key):
            rest = head[len(key):]
            if not rest or rest.isalpha():
                return _TO_SOT[key]
            return None
    return None


def _find_code(text: str) -> str | None:
    """文本中找片式尺寸码(词边界,长码优先)。"""
    for code in _CHIP_CODES:
        if re.search(r"(?<![0-9])" + code + r"(?![0-9])", text):
            return code
    return None


def _category_prefix(context: str) -> str | None:
    """标题/描述中的类别词 → 片式前缀。"""
    t = context.lower()
    for words, prefix in _CATEGORY_WORDS:
        if any(w in t for w in words):
            return prefix
    return None


def match(footprint_title: str, context: str = "") -> str | None:
    """立创封装名(+元件标题/描述上下文)→ KiCad 内置 lib_id 候选。"""
    title = (footprint_title or "").strip()
    if not title:
        return None
    head = title.split("_")[0].strip().upper()

    # 1) SOT / SOD(在剥离尺寸后缀的形态段上匹配)
    m = _match_sot(head)
    if m:
        return m

    # 2) 形态段带 R/C/L 前缀 + 尺寸码(R0603 / C0805 / L1210)
    m = _CHIP_PREFIX_RE.match(head)
    if m:
        prefix, code = m.groups()
        metric = _CHIP_METRIC.get(code)
        if metric:
            return f"{_CHIP_LIB[prefix]}:{prefix}_{code}_{metric}"

    # 3) 无标准前缀:尺寸码(形态段优先,全文兜底)+ 类别词补前缀
    code = _find_code(head) or _find_code(title)
    if code:
        prefix = _category_prefix(context or title)
        if prefix:
            metric = _CHIP_METRIC[code]
            return f"{_CHIP_LIB[prefix]}:{prefix}_{code}_{metric}"

    return None
