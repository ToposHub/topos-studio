# L00 基线记录 · 2026-10-25

> 计划：`docs/upgrade_plan_L_content_library_2026-10-24.md` §8 L00。
> 编制时观测 HEAD（计划文档 v1）：35a9ff904；本基线为开工重新记录。

## 仓库与环境

| 项 | 值 |
|---|---|
| 仓库根 | `/Users/testuser/Documents/vscode/topos_1`（计划文档所写 topos-color-multitask/topos-color01 为编制机路径；本机解析为本根，源码相对路径口径一致） |
| 分支 | `codex/project-insights` |
| 开工 HEAD | `70bfe5fee` |
| 工作区 | clean（无未提交内容需保护） |
| Python | 3.12.14（`.venv`，解释器 `.venv/bin/python`） |
| Qt | 6.11.0（PyQt6） |
| ModernGL | 5.12.0 |
| pytest | 9.1.1（`pytest.ini` testpaths 含 `tests/`） |
| OS | macOS darwin 24.3.0 x64 |
| GPU（standalone ctx 探测） | GL 4.1 ATI-6.1.13 / AMD Radeon RX 6800 OpenGL Engine / ATI —— `gpu_renderer` 标记测试可真实执行 |

## 目录租约确认（§2.1）

开工时 `content_staging/`、`tests/content_library_v1/`、`docs/content_library_v1/`、
`docs/validation/upgrade_L/` 均不存在，未被他人占用；本基线提交创建全部租约目录
（`content_staging/library_v1/README.md` 记录边界红线）。

## 关联计划门控实况（影响 L 线声明口径）

依据 `docs/upgrade_plan_M_mac_2026-10-24.md`（v5）与 git 实况（详见台账附录）：

| 门 | 状态 | 对 L 线的含义 |
|---|---|---|
| M05.1 调色链顺序 | 已收口（commit 58368f343） | L02 可先验单效果；生产跨页链发布声明仍按 M05.2 载荷矩阵未完成口径 |
| M07.1 插件清单 | 已收口（commit cc5446e7e） | L01 身份协议复用 `plugin_identity.py` 既有 8 态状态机，不新增第二套锁 |
| M13 关键帧/Edit UI | 未开始 | L05 的动画模式仅按"已验能力"启用；基础静态效果不等 M13 |
| M19.1 类型化色彩变换 | 已落地（commit 2472be4cd，M 计划文档行尚未勾选） | F01/F05 色彩敏感项按 M19.1 已验子集声明；M19.2/19.4 未验不引用 |
| M20 归档依赖闭包 | 未开始 | 外部位移图/纸纹/字体等外部资源闭包整体后置；无外部资源算子不被阻塞 |
| M21 输出发布事务 | 未开始 | 成片发布走既有流程声明；L22 只验包内可恢复 |
| W 线 | 全部未开始 | Windows 复验项如实分列保留 |
| T 线 | 14/14 完成 | T 独占目录（`src/features/edit/insights/`、`tests/edit/insights/` 等）不再触碰 |

## 本次盘点方法与限制

- 方法：四个并行只读 Explore 扫描（plugins 全量 / Edit 转场链 / Composite 节点体系 /
  共享效果 ABI + M/W/T 状态），关键承重事实经人工抽查源码复核：
  - `plugins/Duotone_Toner.tglsl`（F16 基座参数）；
  - `src/features/edit/core/transition_mask.py:185-190`（slide 复用 wipe mask，双源不动）；
  - `src/features/shared/effects/dctl_wrapper.py` 全文（无参数名改写/别名——L02 接缝证实）。
- 限制：本轮**未运行** GPU 渲染、Windows、安装包；数量描述当前 checkout 的静态盘点，
  不代表全数可用。真实 GPU 编译/像素证据从 L02 起补。
