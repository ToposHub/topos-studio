# 双语对照表 / Bilingual Section Map

> getstarted · 计划 04 P3（2026-09-28）。本表定义 `README.md`（简体中文）与 `README_EN.md`（English）的**逐节对应关系**，以及 README 瘦身后细节内容的迁移去向。两份 README 与本目录各文档的双语半区（"中文" / "English"）须保持信息等价；修改任一侧时须同步另一侧并更新本表。
> 维护规则：新增/移动章节须同步更新本表（依据 `.clinerules` 与 [docs/INDEX.md](../INDEX.md) 维护规则）。

## 1. README 逐节对应（README.md ↔ README_EN.md）

| # | README.md（中文） | README_EN.md（English） |
|---|---|---|
| 标题 | Topos - 专业图像/视频编辑套件（语言切换行 + 简介 + 读音 + 兼容性声明 + 截图位） | Topos — Professional Image and Video Editing Suite（language switch line + introduction + pronunciation + compatibility + screenshot slot） |
| 1 | 产品变体 | Product Editions |
| 2 | 快速开始 | Quick Start |
| 3 | 核心特性速览 | Feature Highlights |
| 4 | 文档导航 | Documentation Navigation |
| 5 | 兼容性与环境 | Compatibility and Environment |
| 尾 | 项目信息（版本、测试规模） | Project Information (version, test suite) |

## 2. 旧 README 章节迁移去向（内容不丢失对照）

旧 README.md（632 行）/ README_EN.md（498 行）各章节的去向：

| 旧章节（README.md / README_EN.md） | 新去向 |
|---|---|
| 兼容性声明 / Compatibility | README 保留一段摘要；全文 → [installation.md](installation.md) §1 |
| §1 产品变体 + 启动方式 / Product Editions + Launching | 产品表保留在 README §1；启动命令 → README §2 快速开始 + [installation.md](installation.md) §3 |
| §2 项目核心架构 / High-Level Architecture | 架构图与 GPU 后端策略 → [features-overview.md](features-overview.md) §2 |
| §3 目录结构说明 / Repository Layout | [development.md](development.md) §1 |
| §4 核心模块详解（各子系统）/ Core Modules | [features-overview.md](features-overview.md) §3 |
| §4 FeatureModule / ProductPreset 接口 | [development.md](development.md) §2–3 |
| §5 主要功能特性 / Features | 一览保留在 README §3；完整清单 → [features-overview.md](features-overview.md) §4 |
| §6 插件系统 / Plugin System | [development.md](development.md) §4 |
| §7 开发流程 / Development Workflows | [development.md](development.md) §5（原生编解码器部分 → [building.md](building.md) §1） |
| §8.1 架构迁移 / Moving Between CPU Architectures | [installation.md](installation.md) §4 |
| §8.2–8.5 平台注意事项 / Cross-Platform Notes | [faq.md](faq.md) §1–4 |
| §8.6 关键开发注意事项 / Development Guidelines | [development.md](development.md) §6 |
| §9 测试与开发环境 / Development and Tests | [building.md](building.md) §2 |
| §10 文档索引 / Documentation Index | README §4 文档导航（指向 docs/INDEX.md）；开发规范部分 → [development.md](development.md) §7 |

## 3. getstarted 文档内部双语对照（每篇均为"中文在前 / English 在后"双半区）

| 文档 | 中文半区章节 | English 半区章节 |
|---|---|---|
| [installation.md](installation.md) | 1 兼容性声明 · 2 安装步骤 · 3 启动方式 · 4 架构迁移须知 · 5 下一步 | 1 Compatibility · 2 Installation · 3 Launching Topos · 4 Moving Between CPU Architectures · 5 Next Steps |
| [building.md](building.md) | 1 原生编解码器构建与测试门禁 · 2 Python 测试与开发环境 · 3 打包（PyInstaller） · 4 基准与性能验证工具 | 1 Building and Testing the Native Codec · 2 Python Tests and Development Environment · 3 Packaging (PyInstaller) · 4 Benchmark and Verification Tools |
| [faq.md](faq.md) | 1 macOS Apple Silicon · 2 macOS 26 (Tahoe) · 3 Windows · 4 Linux · 5 其他常见问题 | 1 macOS Apple Silicon · 2 macOS 26 (Tahoe) · 3 Windows · 4 Linux · 5 Other Common Issues |
| [features-overview.md](features-overview.md) | 1 产品变体 · 2 项目核心架构 · 3 核心模块 · 4 主要功能特性（4.1–4.9） · 5 深入阅读 | 1 Product Editions · 2 High-Level Architecture · 3 Core Modules · 4 Features (4.1–4.9) · 5 Further Reading |
| [development.md](development.md) | 1 目录结构说明 · 2 特性模块系统 · 3 产品预设 · 4 插件系统 · 5 开发流程 · 6 关键开发注意事项 · 7 开发规范文档 | 1 Repository Layout · 2 Feature Module System · 3 ProductPreset · 4 Plugin System · 5 Development Workflows · 6 Development Guidelines · 7 Development References |

## 4. 等价性验证方式

1. 本表 §1 逐行核对两份 README 的章节数量与顺序一致（当前各 5 节 + 标题区 + 尾部信息）。
2. 本表 §3 核对每篇 getstarted 文档的中文/English 半区章节一一对应、顺序一致。
3. 数字与事实抽查：版本号（3.0 / `pyproject.toml`）、产品数量（4 产品 + 2 CLI）、节点数（93）、测试规模（1400+/1465+ 文件、2.2 万+ 用例）、语言数（10）在两侧表述一致（中文惯例计数与英文惯例计数如有出入，以各自 README 现文口径为准）。
