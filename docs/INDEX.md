# Topos Studio 文档索引

新文档放入对应主题子目录。`docs/` 根目录保留此索引。

| 目录 | 内容 |
|---|---|
| [入门与维护](getstarted/) | 安装、构建、开发、常见问题和出厂布局 |
| [架构](architecture/) | 模块边界、[HDR 数学契约](architecture/hdr_math_contract.md)、工程约定、[架构决策](architecture/adr/)和 Metal 迁移 |
| [开发计划](plans/) | 各模块的进行中计划、未结事项和实施交接 |
| [插件](plugins/) | [插件开发指南](plugins/PLUGIN_DEV_GUIDE.md)、规格、示例和本地化 |
| [审查](audit/) | 审计结论及仍用于跟踪的问题证据 |
| [验证](validation/) | 回归基线、手工验收、跨平台验证和[调色对照流程](validation/resolve_comparison_workflow.md) |
| [基准](benchmarks/) | 性能测量、[基准分析图谱](benchmarks/reports/2026-09-28/ATLAS.md)和回归阈值 |
| [编解码](codec/) | 码率、画质、解码及导出协议与决策 |
| [图像](image/) | [Topos Image SDK](image/SDK.md)、格式规范和决策记录 |
| [视频](video/) | Topos 视频格式阶段记录及[发布能力矩阵](video/V8_V9_release_matrix.md) |

## 常用入口

- [构建指南](getstarted/building.md)与[产品测试矩阵](validation/product_test_matrix.md)
- [综合审查整改台账](plans/2026-09-28-multi-machine-program/REMEDIATION_PLAN.md)
- [Windows 性能优化](plans/2026-11-24-windows-resolve-performance-plan.md)与 [macOS 播放和交付复测](validation/2026-11-25-macos-h264-main10-regression.md)
- [视频色彩管理升级](plans/2026-11-25-video-color-management-upgrade.md)与[图层和节点实施](plans/2026-11-22-composite-layer-node-implementation.md)
