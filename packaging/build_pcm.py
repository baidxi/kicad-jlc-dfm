"""构建 KiCad PCM 包(插件管理器可从文件安装)。

产出:dist/lceda-kicad-plugin-<version>-pcm.zip
  zip 根:
    metadata.json                          # PCM v2 包元数据
    plugins/lceda-kicad-plugin/            # API 插件(KiCad 安装到 plugins/)
        plugin.json  icon.png  lceda_plugin_entry.py  README.md
        lceda_plugin/                      # 主代码(打包时从工作区拷贝)
        kipy/                              # IPC 绑定(随包分发,免装 kicad-python)

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
KIPY_SRC = APP_ROOT / "kicad-python" / "kipy"
DIST = APP_ROOT / "dist"


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
    for f in ("plugin.json", "icon.png", "lceda_plugin_entry.py", "README.md"):
        src = PLUGIN_SRC / f
        if src.exists():
            shutil.copy2(src, plugin_dir / f)
        else:
            print(f"[warn] 缺少 {f}")

    # 主代码 + kipy 随包分发
    # 目标目录名固定 lceda_plugin(合法包名);源码来自用户目录 lceda-plugin
    n1 = _copy_tree(_app_source_dir(), plugin_dir / "lceda_plugin")
    n2 = _copy_tree(KIPY_SRC, plugin_dir / "kipy") if KIPY_SRC.is_dir() else 0
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--version", help="Override package version (e.g. 1.2.0 from git tag)")
    args = ap.parse_args()
    out = build(args.version)
    emit_repository_entry(out)
    if args.no_validate:
        return 0
    return 0 if validate(out) else 1


if __name__ == "__main__":
    raise SystemExit(main())
