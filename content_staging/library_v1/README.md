# content_staging/library_v1 —— 内容库 L 计划第三机暂存区

> 计划：`docs/upgrade_plan_L_content_library_2026-10-24.md`（§2.1 文件所有权）。
> 本目录由第三机（L 线）独占写入；Mac 主集成者只在发布时从这里取组件放入生产路径。
> 开工时间：2026-10-25；开工时 `content_staging/` 不存在，目录未被占用。

## 边界（红线）

1. **本暂存区不属于生产插件发现路径**：
   - Color 页 `PluginManager` 只扫描 `<repo>/plugins/*.tglsl|*.tai`（非递归）；
   - Composite `NodePluginManager` 只扫描 `<repo>/plugins/node_plugins/` 与 `~/.topos/plugins/node_plugins/`；
   - 本目录不会被任何生产 loader 自动加载。测试 parser 时传显式暂存路径，
     不得用生产 reload 替换正在运行的全局 registry。
2. **不修改共享文件**：`plugin_manager.py / shader_compiler.py / effect_catalog.py /
   dctl_wrapper.py / effect_pass_runner.py / plugin_identity.py / transition_catalog.py /
   custom_transitions.py / transition_mask*.py / gpu_transition_shaders.py /
   node_plugin.py / node_engine.py / node_capabilities.py / base_evaluator.py /
   shaders.py / graph_* / composite_export_renderer.py / 宿主菜单与 i18n / 工程 schema /
   打包与 CI` 及既有 35 个 `.tglsl`——这些属于 Mac 主集成者串行修改（§2.2）。
   对这些文件的**窄适配提案**以 `patches/` 下的草案文本交付，不直接改。
3. **状态诚实**：本目录交付物状态只能是 `planned / component_ready`；
   `integrated` 起要接宿主真实运行（Mac 集成者），`validated/released` 要过 L20–L22。

## 目录

| 路径 | 用途 |
|---|---|
| `specs/` | 逐项规格（L01 契约字段）、L00 台账、身份/能力 manifest、接缝提案 |
| `filters/` | 单输入算法源/适配描述（F01–F20，每项独立子目录） |
| `transitions/` | 双源 CPU 核、GLSL/MSL 草稿及参数映射（X01–X18） |
| `nodes/` | 节点类/端口描述、执行器小补丁草案（C01–C21） |
| `presets/` | P01–P80 规范载荷、宿主格式转换输入与资源引用 |
| `references/` | 独立数学参考（CPU oracle，不调用被测 GPU 函数生成预期） |
| `fixtures/` | 黄金输入/手算预期（hash 齐全、可再分发） |
| `previews/` | 实际算法生成的预览素材与 manifest |
| `patches/` | 给 Mac 主集成者的最小可审阅变更草案（文本，不直接生效） |

## 测试与文档

- `tests/content_library_v1/`：自有 focused/契约/视觉测试；不改父 conftest 与共享夹具。
- `docs/content_library_v1/`：使用说明、算法区别、平台交接、发布记录。
- `docs/validation/upgrade_L/`：每包基线/结果/能力矩阵与证据索引。

## 发布路径（拟定，由 L02–L04 定稿）

- 单输入 `.tglsl` → `plugins/LIB1_<slug>.tglsl`（非递归 loader 直接可见）。
- 节点 → `plugins/node_plugins/topos_library_v1/` 包。
- 转场 → `src/features/edit/transition_packs/library_v1/`（经现有转场管线窄接缝）。
- 同一核供多页使用时由 Mac 集成者放入 shared effects 层合适位置；
  上层不得反向导入 Composite 内部代码。
