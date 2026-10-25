# lceda-kicad-plugin

立创商城(LCSC)元件 → KiCad 10.99 集成:在线搜索、放置到原理图、批量修改封装、
封装与 3D 模型自动下载入库并关联。

架构与设计决策见 [../plans/lceda-kicad-plugin-implementation-plan.md](../plans/lceda-kicad-plugin-implementation-plan.md)。

## 环境要求

- KiCad 10.99(IPC API 开启,kipy 0.9.0.dev0)
- Python 包:`PySide6` `httpx` `cryptography` —— **首次启动自动安装**(见下);
  离线环境可提前 `pip install --user PySide6 httpx cryptography kicad-python`

## 安装

**方式一:KiCad 插件管理器(PCM,推荐)**

```bash
python3 packaging/build_pcm.py        # 构建并校验 → dist/lceda-kicad-plugin-<版本>-pcm.zip
```

KiCad → 插件管理器 → ⋮ → 「从文件安装…」→ 选择 zip → 应用。
重启 KiCad 后 eeschema 工具栏出现橙色 L 图标。包自包含(含 kipy);
**首次点击图标时**入口自动检测缺失依赖并用 pip 补齐 `PySide6` / `httpx` /
`cryptography`(约 200 MB,需联网,可能持续数分钟,期间无窗口属正常,
装完直接进入 GUI,无需重启 KiCad)。全过程与失败原因记录在插件目录
`lceda-plugin-error.log`;设环境变量 `LCEDA_NO_AUTOINSTALL=1` 可关闭自动安装。
(kipy 的传递依赖 pynng/protobuf/zstandard/jsonschema 缺失时同样自动补齐。)
国内网络可设 `LCEDA_PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`
走清华镜像加速。系统无 python3-pip 时(如全新 Ubuntu 24.04)由随包分发的
`pip.pyz` 兜底引导,无需预装任何东西。

**方式二:开发者运行**(改动代码即时生效)

```bash
python3 -m lceda_plugin.main
```

启动时自动连接运行中的 KiCad(原理图需打开);未连接则降级为纯下载入库模式。
> 注意:工作区还注册了开发用入口(~/.local/bin/lceda-kicad-plugin、应用菜单、
> plugins/ 下的手工目录),与 PCM 安装并存时请只保留一种,避免点到旧实例。

> **已注册为 KiCad 10 API 插件**:安装位置
> `~/.local/share/kicad/10.99/plugins/lceda-kicad-plugin/`(含 `plugin.json` 清单、
> `icon.png` 图标、`lceda_plugin_entry.py` 入口,kipy packaging validate 通过)。
> **重启 KiCad 后**,eeschema 工具栏会出现橙色 L 图标按钮(schematic scope),
> 点击即启动本 GUI(KiCad 注入 `KICAD_API_SOCKET/TOKEN`,GUI 内自动连接)。
>
> 注意:`plugins/` 目录识别的是这种 **API 插件结构**(清单 + 入口),直接拷 Python
> 包进去不会被识别;action plugin(`RegisterPlugin`)路线在本机不可行——eeschema
> 无此机制且 10.99 未启用 SWIG。API 插件要求 KiCad 启动时开启 API 服务器(10.99 默认开启)。
>
> 图标/清单修改后需重启 KiCad 生效;入口脚本仅做依赖引导,主程序仍在
> `/home/juno/tools/kicad-jlc-dfm/lceda_plugin/`,改代码无需重装。

## 功能

### 搜索放置
1. 输入关键词或立创编号(如 `AO3400` / `C20917` / `0603 100nF`)搜索
2. 选中结果 → 「放置到原理图」:符号立即放置,并写入
   `Footprint = LCSC:<封装名>`、`Supplier = LCSC`、`Supplier Part = <编号>`
3. 封装 + 3D 模型后台下载到全局库并自动关联(`.kicad_mod` 注入 `(model)` 子句)

### 批量改封装
- **A 在线按 Supplier Part 更新**:读取原理图元件 → 勾选 → 按编号在线取对应封装/3D
  入库 → 单次 commit 批量改 Footprint;无编号行跳过并出报告
- **B 手动指定**:离线批量改字段
- **C 绑定放置页选中元件**:通用件(R/C/L/三极管/MOSFET)手动搜索后一键绑定
  封装 + 添加 Supplier/Supplier Part 属性

## 全局库布局

| 内容 | 位置 |
|---|---|
| 符号库 | `~/.local/share/kicad/<版本>/symbols/LCSC.kicad_sym` |
| 封装库 | `~/.local/share/kicad/<版本>/footprints/LCSC.pretty/` |
| 3D 模型 | `~/.local/share/kicad/<版本>/3dmodels/`(.step/.wrl 按内容嗅探) |
| 库表 | `~/.config/kicad/<版本>/sym-lib-table`、`fp-lib-table`(幂等注册) |

路径可在 `~/.config/lceda-kicad-plugin/settings.json` 覆盖。

## 冒烟测试(不触碰真实库)

```bash
python3 tests/smoke_ingest.py AO3400
```

## 已验证(2026-10,本机 KiCad 10.99)

- 真实链路:AO3400(C20917)与 0603 电容全流程,STEP 2.7MB 正确落盘
- 模式 A 写入联调:原理图 R4(C778420)→ Footprint `LCSC:R0201` 生效,
  Supplier/Supplier Part 保持,3D 关联写入封装文件
- busy 重试:KiCad 忙时自动等待重试

## 已知限制

- 转换器几何未经官方导入器全量对拍(规划阶段 0 遗留):复杂焊盘/多形状 FILL
  可能与立创预览有差异,发现问题时请用 `tests/smoke_ingest.py` 复现并报告
- 放置位置固定在图纸原点,放置后手动拖动到位
- 商城库存/价格查询未实现(WAF 依赖,规划明确裁剪)
