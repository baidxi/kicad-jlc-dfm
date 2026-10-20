# LCSC → KiCad(立创元件库助手)

立创商城(LCSC)元件与 KiCad 的一体化集成插件。

## 功能

- **在线搜索**:关键词或立创编号(智能识别 `C` 编号优先精确匹配)
- **单击预览**:搜索结果内嵌预览面板(实物图 + 参数简介)
- **交互式放置**:点「放置到原理图」→ 窗口最小化 → 元件跟随鼠标,左键落位(需 KiCad 含 interactive API 扩展;否则直接落在网格位后按 M 移动)
- **字段全自动**:Footprint / Supplier / Supplier Part / Value 随放置自动写入
- **封装与 3D 自动入库**:符号、封装、STEP/WRL 下载到全局库,3D 模型自动关联到封装
- **批量修改封装**(三种模式):
  - A:按 Supplier Part 编号在线更新选中元件
  - B:从全部已注册封装库(含 KiCad 内置)选择器中手动指定
  - C:行内「搜索替换」——在线搜索立创元件并绑定(带预览)
- **联动**:单击表格行 → KiCad 原理图自动跳转并高亮该元件

## 安装(插件管理器从文件安装)

1. 获取发布包 `lceda-kicad-plugin-<版本>-pcm.zip`(GitHub Releases 下载,或本地构建,见下)
2. KiCad → **插件管理器**(Tools → Plugin and Content Manager)→ 右上角 **「从文件安装…」**→ 选择 zip → 应用
3. **重启 KiCad**,打开原理图,eeschema 工具栏出现橙色 **L** 图标,点击即启动

运行时依赖(系统 Python 可见即可,通常已具备):

```bash
pip install --user PySide6 httpx cryptography
```

> `kipy`(IPC 绑定)已随包分发,无需安装。
> 需要 KiCad 10.99+ 且 IPC API 已启用(10.99 默认开启)。

## 本地构建发布包

```bash
python3 packaging/build_pcm.py            # 构建并校验 → dist/*.pcm.zip
python3 packaging/build_pcm.py --version 1.2.0   # 指定版本号
```

自动生成 `dist/*.repository-entry.json`(提交官方 kicad/addons 插件仓库所需的索引条目)。

## 开发者运行(改代码即时生效)

```bash
python3 lceda_plugin_main.py
```

主代码目录为 `lceda-plugin/`(连字符),入口脚本会把它注册为虚拟包 `lceda_plugin`。

## 全局库布局

| 内容 | 位置(可在 `~/.config/lceda-kicad-plugin/settings.json` 覆盖) |
|---|---|
| 符号库 | `~/Documents/kicad/lcsc/lcsc.kicad_sym`(默认昵称 `lcsc`) |
| 封装库 | `~/Documents/kicad/lcsc/lcsc.pretty/` |
| 3D 模型 | `~/.local/share/kicad/<版本>/3dmodels/` |

## 交互放置扩展(可选,需源码编译 KiCad)

完整鼠标跟随体验依赖给 KiCad API 增加的 `PlaceSymbolFromLibrary.interactive` 扩展
(proto 字段 + eeschema handler 分支,见 `kicad/api/proto/schematic/schematic_commands.proto`
与 `eeschema/api/api_handler_sch.cpp`)。无此扩展时元件直接落在网格位,按 M 移动。

## 已知限制

- Wayland 下程序无法控制独立窗口位置,故预览/放置均采用页内面板与最小化方案
- 转换几何未经官方导入器全量对拍,个别复杂封装如异常请用 `tests/smoke_ingest.py <编号>` 复现
