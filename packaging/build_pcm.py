"""构建 KiCad PCM 包(插件管理器可从文件安装)。

产出:dist/lceda-kicad-plugin-<version>-pcm.zip
  zip 根:
    metadata.json                          # PCM v2 包元数据
    plugins/lceda-kicad-plugin/            # API 插件(KiCad 安装到 plugins/)
        plugin.json  icon.png  lceda_plugin_entry.py  README.md
        lceda_plugin/                      # 主代码(打包时从工作区拷贝)
        kipy/                              # IPC 绑定(随包分发,免装 kicad-python)
        pip.pyz                            # pip zipapp(无 pip 环境的依赖引导)

用法:
    python3 packaging/build_pcm.py             # 打包 + kipy validate
    python3 packaging/build_pcm.py --no-validate
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent
APP_ROOT = PKG_ROOT.parent
PLUGIN_SRC = PKG_ROOT / "content" / "plugins" / "lceda-kicad-plugin"
DIST = APP_ROOT / "dist"


def _has_interactive(kipy_dir: Path) -> bool:
    pb = kipy_dir / "proto" / "schematic" / "schematic_commands_pb2.py"
    try:
        return "interactive" in pb.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


PIP_PYZ_URL = "https://bootstrap.pypa.io/pip/pip.pyz"


def _fetch_pip_pyz(plugin_dir: Path) -> bool:
    """下载官方 pip zipapp 随包分发(约 2 MB)。

    全新 Ubuntu 24.04 等系统无 python3-pip 且 ensurepip 被剥离,
    KiCad 内置精简 Python 亦然;插件入口首次运行的依赖引导
    (pip.pyz install --user)以此兜底。
    """
    dest = plugin_dir / "pip.pyz"
    try:
        import urllib.request

        urllib.request.urlretrieve(PIP_PYZ_URL, dest)
        print(f"pip.pyz {dest.stat().st_size // 1024} KB(随包分发)")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] pip.pyz 下载失败({exc});"
              "无 pip 的目标机将无法自动安装依赖")
        return False


def _kipy_src() -> Path:
    """kipy 源目录:优先用户 fork 的优化版(含 interactive 扩展)。

    顺序:
      1. 已安装的 kipy —— CI 从用户 fork 安装,含交互放置优化
      2. 仓库内 kicad-python/kipy —— 本地开发副本(同样含扩展)
      3. 兜底:任意已安装 kipy
    """
    import kipy  # noqa: PLC0415

    installed = Path(kipy.__file__).parent
    repo_copy = APP_ROOT / "kicad-python" / "kipy"

    if _has_interactive(installed):
        return installed
    if repo_copy.is_dir() and _has_interactive(repo_copy):
        return repo_copy
    return installed


def _app_source_dir() -> Path:
    """主代码源目录:优先 `lceda-plugin`(用户目录名),回退 `lceda_plugin`。"""
    for name in ("lceda-plugin", "lceda_plugin"):
        p = APP_ROOT / name
        if p.is_dir():
            return p
    return APP_ROOT / "lceda-plugin"

# 打包时排除的文件模式
EXCLUDE = ["__pycache__/*", "*.pyc", "*.pyo", "*.log", ".git/*"]


def _version() -> str:
    init = (_app_source_dir() / "__init__.py").read_text(encoding="utf-8")
    for line in init.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


def _copy_tree(src: Path, dst: Path) -> int:
    """拷贝目录(应用排除规则),返回文件数。"""
    count = 0
    for root, _dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        for f in files:
            relf = os.path.join(rel, f) if rel != "." else f
            if any(fnmatch.fnmatch(relf, pat) for pat in EXCLUDE):
                continue
            target = dst / relf
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(os.path.join(root, f), target)
            count += 1
    return count


def build(version: str | None = None) -> Path:
    version = version or _version()
    work = DIST / "_work" / "lceda-kicad-plugin"
    if (DIST / "_work").exists():
        shutil.rmtree(DIST / "_work")

    plugin_dir = work / "plugins" / "lceda-kicad-plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)

    # 静态文件
    # 注意:不放 requirements.txt —— KiCad 10.99 的 INSTALL_REQUIREMENTS
    # 会在启动时用 venv 的 pip 安装它,而 Ubuntu 24.04 等缺 python3-venv 的
    # 系统上 venv 是残缺的(无 pip),安装失败会导致插件永不 ready、
    # 工具栏按钮不出现(死锁)。依赖改由入口脚本 pip.pyz 兜底安装。
    for f in ("plugin.json", "icon.png", "lceda_plugin_entry.py", "README.md"):
        src = PLUGIN_SRC / f
        if src.exists():
            shutil.copy2(src, plugin_dir / f)
        else:
            print(f"[warn] 缺少 {f}")

    # 主代码 + kipy 随包分发
    # 目标目录名固定 lceda_plugin(合法包名);源码来自用户目录 lceda-plugin
    n1 = _copy_tree(_app_source_dir(), plugin_dir / "lceda_plugin")
    n2 = _copy_tree(_kipy_src(), plugin_dir / "kipy")
    _fetch_pip_pyz(plugin_dir)
    print(f"主代码 {n1} 个文件, kipy {n2} 个文件")

    # PCM 元数据(archive 内不得含 download_* 键)
    meta = json.loads((PKG_ROOT / "metadata.json").read_text(encoding="utf-8"))
    meta["versions"][0]["install_size"] = sum(
        f.stat().st_size for f in plugin_dir.rglob("*") if f.is_file())
    (work / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # zip(根 = metadata.json + plugins/)
    out = DIST / f"lceda-kicad-plugin-{version}-pcm.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _dirs, files in os.walk(work):
            for f in files:
                full = Path(root) / f
                zf.write(full, full.relative_to(work))
    print(f"打包完成: {out}  ({out.stat().st_size // 1024} KB)")
    return out


def validate(pkg: Path) -> bool:
    print("=== kipy validate ===")
    proc = subprocess.run(
        [sys.executable, "-m", "kipy.packaging.cli", "validate", str(pkg)],
        capture_output=True, text=True)
    print(proc.stdout or proc.stderr)
    return proc.returncode == 0


def emit_repository_entry(out: Path) -> None:
    """输出 kicad/addons 官方仓库索引所需的包条目片段。

    提交官方插件仓库时:把 zip 发布到 GitHub Release,把本条目(填入
    download_url 后)合并进 kicad/addons 仓库的 packages/repository.json。
    """
    meta = json.loads((PKG_ROOT / "metadata.json").read_text(encoding="utf-8"))
    import hashlib

    data = out.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    entry = {
        "$schema": "https://go.kicad.org/pcm/v2/schema/package",
        "identifier": meta["identifier"],
        "name": meta["name"],
        "description": meta["description"],
        "description_full": meta.get("description_full", ""),
        "type": meta["type"],
        "author": meta["author"],
        "license": meta["license"],
        "resources": meta.get("resources", {}),
        "tags": meta.get("tags", []),
        "versions": [
            {
                "version": v["version"],
                "status": v["status"],
                "kicad_version": v["kicad_version"],
                "download_url": f"<GITHUB_RELEASE_URL>/{out.name}",
                "download_sha256": sha,
                "download_size": len(data),
                "install_size": v.get("install_size", 0),
                **({"runtime": v["runtime"]} if "runtime" in v else {}),
            }
            for v in meta["versions"]
        ],
    }
    out.with_suffix(".repository-entry.json").write_text(
        json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"仓库条目: {out.with_suffix('.repository-entry.json')}")
    return entry


def emit_repository_index(out: Path, entry: dict) -> None:
    """输出完整个人仓库索引 repository.json。

    把它与 zip 一起放到任意 HTTP 服务(如 GitHub Pages),即可在
    KiCad 插件管理器 → 管理仓库 中添加该 URL,实现在线安装与更新。
    注意:download_url 需改为 zip 的实际 HTTP 地址。
    """
    index = {
        "$schema": "https://go.kicad.org/pcm/v2/schema/repository",
        "name": "LCSC KiCad Integration (personal repository)",
        "packages": [entry],
    }
    idx = out.with_name("repository.json")
    idx.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"个人仓库索引: {idx}  (download_url 需改为 zip 的实际 HTTP 地址)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--version", help="Override package version (e.g. 1.2.0 from git tag)")
    args = ap.parse_args()
    out = build(args.version)
    entry = emit_repository_entry(out)
    # 自建仓库索引中的 zip 地址:发布到 HTTP 服务后改为实际 URL
    entry["versions"][0]["download_url"] = out.name
    emit_repository_index(out, entry)
    if args.no_validate:
        return 0
    return 0 if validate(out) else 1


if __name__ == "__main__":
    raise SystemExit(main())
