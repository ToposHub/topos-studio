# [ARCH-11] 将 file_explorer/plugins 定为常规包。
#
# 此前本目录无 __init__.py：仓库根目录也有一个 plugins/（存 .tglsl 着色器
# 资源，非 Python 包），两者在 sys.path 上合并为同一命名空间包——任何一方
# 将来放入同名模块都会互相遮蔽。定为常规包后 `import plugins` 确定性地
# 解析到本目录（常规包优先于命名空间包片段）。
#
# 注意：不要在本文件 import 各插件或 Flask——本包被宿主进程 import 时
# 必须保持零副作用（插件由 plugin_manager 在 ensure_runtime 里按需加载）。
