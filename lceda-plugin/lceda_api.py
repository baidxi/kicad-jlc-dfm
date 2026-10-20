"""立创 EDA 数据链路:搜索 / 库文档下载解密 / STEP 下载。

链路(自 lceda_step_downloader C# 项目逆向,已在本机 KiCad 环境规划中复核):
1. 搜索   GET https://pro.lceda.cn/api/szlcsc/eda/product/list?wd={kw}
2. 文档   GET https://pro.lceda.cn/api/v2/components/{uuid}
          → result.dataStrId + iv(12B hex) + key(32B hex)
          GET dataStrId → AES-256-GCM 密文(尾部 16B auth tag)
          解密 → gzip → 标准格式文档(每行一条 ["TYPE", ...] JSON 指令)
3. STEP   GET api/v2/components/{3D uuid} → result.Model3DUuid
          GET https://modules.lceda.cn/qAxj6KHrDKw4blvCG8QJPs7Y/{Model3DUuid}
"""

from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_PRO_BASE = "https://pro.lceda.cn"
_STEP_BASE = "https://modules.lceda.cn/qAxj6KHrDKw4blvCG8QJPs7Y"
_TAG_SIZE = 16

_LCSC_PART_RE = re.compile(r"[Cc]\d{4,}")


class LcedaError(RuntimeError):
    """立创数据链路错误(接口/解密/解压)。"""


@dataclass
class LcedaComponent:
    """一条搜索结果。uuid 字段取值规则与 C# 版一致:attributes 优先,子对象兜底。"""

    uuid: str
    title: str
    part_number: str | None          # product_code,如 "C2977694"
    attributes: dict[str, str] = field(default_factory=dict)
    description: str = ""
    symbol_uuid: str | None = None
    footprint_uuid: str | None = None
    footprint_title: str | None = None
    images: list[str] = field(default_factory=list)   # 元件预览图 URL

    @property
    def model3d_uuid(self) -> str | None:
        v = self.attributes.get("3D Model")
        return v or None

    @property
    def supplier_part(self) -> str | None:
        return self.part_number

    def clean_part_number(self) -> str | None:
        """从 part_number / attributes 中清洗出 C 编号(容忍 'LCSC C123 #备注' 等格式)。"""
        for candidate in (self.part_number, self.attributes.get("Supplier Part")):
            if not candidate:
                continue
            m = _LCSC_PART_RE.search(candidate)
            if m:
                return m.group(0).upper()
        return None


def get_safe_filename(name: str | None) -> str:
    """文件名清洗(对应 C# GetSafeFileName)。"""
    if not name:
        return "unknown"
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("._") or "unknown"


def _result_to_component(item: dict[str, Any]) -> LcedaComponent:
    attrs = item.get("attributes") or {}
    sym = item.get("symbol") or {}
    fp = item.get("footprint") or {}
    images = [i for i in (item.get("images") or []) if isinstance(i, str)]
    return LcedaComponent(
        uuid=item.get("uuid") or "",
        title=item.get("display_title") or item.get("title") or (sym.get("title") if isinstance(sym, dict) else None) or item.get("uuid") or "unknown",
        part_number=item.get("product_code"),
        attributes={k: v for k, v in attrs.items() if isinstance(v, str)},
        description=item.get("description") or "",
        symbol_uuid=(attrs.get("Symbol") or (sym.get("uuid") if isinstance(sym, dict) else None)),
        footprint_uuid=(attrs.get("Footprint") or (fp.get("uuid") if isinstance(fp, dict) else None)),
        footprint_title=(fp.get("display_title") or fp.get("title")) if isinstance(fp, dict) else None,
        images=images,
    )


class LcedaClient:
    """单例会话:高频请求复用连接,避免 TIME_WAIT 累积(C# 版教训)。"""

    # 立创 WAF 按 UA 指纹拦截非浏览器客户端(python-httpx 默认 UA 返回 418)
    _HEADERS = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    }

    def __init__(self, timeout: float = 30.0) -> None:
        self._client = httpx.Client(timeout=timeout, follow_redirects=True,
                                    headers=self._HEADERS)
        self._img_cache: dict[str, bytes] = {}

    def close(self) -> None:
        self._client.close()

    # ── 搜索 ──

    def search(self, keyword: str) -> list[LcedaComponent]:
        resp = self._client.get(f"{_PRO_BASE}/api/szlcsc/eda/product/list",
                                params={"wd": keyword})
        resp.raise_for_status()
        data = resp.json()
        results = data.get("result") or []
        return [_result_to_component(i) for i in results if isinstance(i, dict)]

    def search_by_part_number(self, part_number: str) -> LcedaComponent | None:
        """按 C 编号精确搜索:结果里取 product_code 精确匹配的第一条。"""
        want = part_number.upper()
        for comp in self.search(part_number):
            if (comp.part_number or "").upper() == want:
                return comp
        # 精确匹配失败时返回第一条(搜索接口对编号通常已高度精确)
        results = self.search(part_number)
        return results[0] if results else None

    def smart_search(self, keyword: str) -> list[LcedaComponent]:
        """搜索策略:纯 C 编号输入(C+4 位以上数字)优先按编号精确匹配;
        精确未命中再回退关键词搜索。其余输入按关键词。"""
        kw = keyword.strip()
        if re.fullmatch(r"[Cc]\d{4,}", kw):
            comp = self.search_by_part_number(kw)
            if comp is not None:
                return [comp]
        return self.search(kw)

    # ── 库文档下载(符号/封装) ──

    def fetch_document(self, uuid: str) -> tuple[str, dict[str, Any]]:
        """下载并解密指定 uuid 的库文档,返回 (文本, 详情元数据 dict)。"""
        resp = self._client.get(f"{_PRO_BASE}/api/v2/components/{uuid}")
        resp.raise_for_status()
        detail = (resp.json() or {}).get("result")
        if not detail:
            raise LcedaError(f"组件详情接口返回空结果: {uuid}")

        data_str_id = detail.get("dataStrId")
        iv = detail.get("iv")
        key = detail.get("key")
        if not (data_str_id and iv and key):
            raise LcedaError(f"该文档缺少数据内容(dataStrId/iv/key 为空): {uuid}")

        cipher = self._client.get(data_str_id).content
        if len(cipher) <= _TAG_SIZE:
            raise LcedaError(f"文档数据异常(长度不足): {uuid}")

        aes = AESGCM(bytes.fromhex(key))
        plain = aes.decrypt(bytes.fromhex(iv), cipher, None)  # 密文体+tag 连体传入

        try:
            text = gzip.decompress(plain).decode("utf-8")
        except (OSError, UnicodeDecodeError) as ex:
            raise LcedaError(f"gzip 解压失败: {uuid}: {ex}") from ex
        return text, detail

    # ── 3D 模型(STEP) ──

    def fetch_image(self, url: str) -> bytes:
        """下载元件预览图(协议相对 URL 补 https:;带进程内缓存)。"""
        if not url.startswith("http"):
            url = "https:" + url
        cached = self._img_cache.get(url)
        if cached is not None:
            return cached
        data = self._client.get(url).content
        self._img_cache[url] = data
        return data

    def download_model(self, model3d_uuid: str, dest_dir: Any, base_name: str) -> Any:
        """下载 3D 模型到 dest_dir/<base_name>.<ext>,返回实际写入的 Path。

        修复相对 C# 版的两个问题:
        1. 详情键名为 snake_case 的 ``3d_model_uuid``(非 Model3DUuid)——用错 uuid 会 403;
        2. 部分模型实为 WRL(C# 版硬编码 .step 会让 KiCad 读不了),按内容嗅探定扩展名。
        """
        resolved = model3d_uuid
        try:
            resp = self._client.get(f"{_PRO_BASE}/api/v2/components/{model3d_uuid}")
            resp.raise_for_status()
            detail = (resp.json() or {}).get("result") or {}
            resolved = detail.get("3d_model_uuid") or model3d_uuid
        except httpx.HTTPError:
            pass  # 详情失败不阻塞,走兜底直拼

        blob = self._client.get(f"{_STEP_BASE}/{resolved}").content
        if blob.startswith(b"ISO-10303-21"):
            ext = ".step"
        elif blob.lstrip().startswith(b"#VRML"):
            ext = ".wrl"
        else:
            raise LcedaError(
                f"3D 模型内容异常(非 STEP/WRL): {model3d_uuid} -> {blob[:64]!r}")

        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / (base_name + ext)
        dest_file.write_bytes(blob)
        return dest_file
