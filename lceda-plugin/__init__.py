"""lceda-kicad-plugin:立创商城元件 → KiCad 10.99 集成。

模块:
- settings        KiCad 目录探测与设置持久化
- lceda_api       立创数据链路(搜索/解密/STEP)
- kicad_converter 指令流 → .kicad_sym/.kicad_mod
- lib_manager     全局库文件与库表管理
- pipeline        放置流水线编排(同步符号/后台封装+3D)
- kicad_ipc       kipy(IPC API)封装:放置/批量改字段
- batch_footprint 批量改封装三模式
- main            PySide6 GUI
"""

__version__ = "0.1.0"
