# topos-studio - 专业图像/视频编辑套件

🌐 简体中文 · [English](README_EN.md)

**topos-studio** 是一个基于 **PyQt6** 和 **ModernGL** 开发的高性能非破坏性图像/视频编辑软件套件。采用模块化多产品架构，提供四个专业产品变体和两个 CLI 工具，内置自研 **libtopos_codec** 原生编解码器（C11，`.toos` 中间格式），支持 HDR 处理、ACEScg/OCIO 色彩管理、AI 辅助修复与跨平台 GPU 后端（OpenGL 基线 + Metal 渐进接入）。当前版本 **3.0**（`pyproject.toml`）。

**读音：** Topos /ˈtoʊ.poʊs/（近似“托—坡斯”，重音在第一音节）

## 项目状态与重要说明

*   **开发进度**：项目整体完成度约 **50%**，仍存在大量 bug 与待完善功能，正在持续修复中。欢迎提交 Issue 反馈问题、发起 PR 参与修复——任何形式的贡献都非常欢迎，期待与你一起把这个项目共同完善！
*   **AI 辅助开发声明**：本项目的绝大部分代码由 [智谱 GLM 大模型](https://open.bigmodel.cn/) 辅助开发，人类开发者负责需求定义、架构设计、验收测试与整体整合。
*   **关于 DaVinci Resolve**：本项目的界面布局与交互流程借鉴了 DaVinci Resolve 的使用体验，但**未使用、未复制、未衍生 DaVinci Resolve 的任何代码、素材、字体或资源文件**，全部代码均为基于 PyQt6 / ModernGL 的独立实现。DaVinci Resolve 是 Blackmagic Design Pty. Ltd. 的商标；本项目与 Blackmagic Design 无任何隶属、合作或背书关系。

## 截图

<!-- 截图位：主界面截图待补。建议放置路径 docs/image/screenshots/，例如：
![Topos Studio 主界面](docs/image/screenshots/studio-main.png)
（当前仓库无截图资源，暂留占位，不引用不存在的外链图片）
-->

## 1. 产品变体

topos-studio 采用**产品变体架构**，同一代码库构建四个专业产品：

| 产品 | 功能组合 | 用途 |
|------|----------|------|
| **Topos Studio** | Color + Edit + Composite + Deliver + Teaching Animation | 全功能版本，专业影视后期与动态图形 |
| **Topos Color** | Color + Deliver (轻量) | 调色专注版，摄影师/调色师 |
| **Topos Edit** | Edit + Deliver + Color (查看器) | 剪辑专注版，视频剪辑师 |
| **Topos Composite** | Composite + Deliver + Color (查看器) + Teaching Animation | 合成专注版，特效与动态图形 |

另有两个独立 CLI 工具（随 `pip install -e .` 安装）：**toposctl**（AI 编辑协议 CLI，`read` / `validate` / `preview` 经与 UI/MCP 同一 ToolFacade 路径执行）与 **topos-vault**（素材资产库 CLI：AI 打标、移动后重连、provider 状态）。

## 2. 快速开始

```bash
# 一键启动（自动检测 Python 架构 / GPU / 系统依赖）
./run_topos.command        # macOS
bash run_topos.sh          # Linux
run_topos.bat              # Windows

# 或：安装后按产品启动
pip install -e .
topos-studio               # 或 topos-color / topos-edit / topos-composite

# 或：模块方式
python -m apps.topos_color.main
python src/main.py --product studio   # 统一入口 + 产品参数
```

安装步骤、Python/Qt 版本要求与架构迁移须知详见 [docs/getstarted/installation.md](docs/getstarted/installation.md)。

## 3. 核心特性速览

*   **调色 (Color)**：一级/二级校色、曲线、OCIO v2 + ACEScg 色彩管理、8 模式全 GPU 示波器、GPU 视频降噪、DaVinci 风格跟踪器、Tangent 硬件调色台、LUT
*   **剪辑 (Edit)**：多轨时间线（16 种混合模式）、字幕/标题模板、场景检测、时间线遮挡剔除（实测最高 **5.23×** 加速）、J/K/L 变速播放引擎、专业音频混音台（效果器/自动化/录音）
*   **合成 (Composite)**：**93 个节点**（跟踪/抠像/深度氛围/绘制/粒子/AI/3D 子系统），最高 16K 工程，节点磁盘缓存（`.toos`）
*   **交付 (Deliver)**：H.264/H.265/ProRes/DNxHR/TOPOS 中间编码，VideoToolbox/NVENC/AMF 硬件编码，批量导出与渲染队列，SRT 字幕导出，导出 Manifest 全量验证
*   **AI**：生成/修复/外扩/超分，事务化 AI 编辑（ApprovalGrant 授权），可解释节点契约，统一 AI 技能运行时与可视化工作流编排 `[实验性]`
*   **教学动画 / 动态图形**：场景编辑器 + 多场景页、13 个内置预设、SVG/HTML 双渲染后端、Figma 式自动布局、AI 辅助生成（fail-closed）
*   **原生媒体格式**：`.toos` 图片（整数/float16/RAW 全档位）与 TOPOS 视频（七帧内档 + LP + RAW 档），Topos RAW 相机 RAW 编解码
*   **脚本与自动化**：Python 脚本 API（`topos` 对象）、`.tglsl`/`.tai` 插件体系、10 种界面语言

各特性的完整清单与实测数据见 [docs/getstarted/features-overview.md](docs/getstarted/features-overview.md)。

## 4. 文档导航

**上手（docs/getstarted/）**

| 文档 | 内容 |
|------|------|
| [installation.md](docs/getstarted/installation.md) | 安装与环境：依赖、启动方式、架构迁移 |
| [features-overview.md](docs/getstarted/features-overview.md) | 功能总览：产品变体、架构、核心模块、完整特性清单 |
| [faq.md](docs/getstarted/faq.md) | 常见问题：macOS / Windows / Linux 平台问题表 |
| [building.md](docs/getstarted/building.md) | 构建与打包：原生编解码器、测试、PyInstaller |
| [development.md](docs/getstarted/development.md) | 开发指南：目录结构、插件、开发流程与规范 |
| [bilingual-map.md](docs/getstarted/bilingual-map.md) | 中英文章节对照表（双语等价验证） |

**深入**

*   全部文档索引：[docs/INDEX.md](docs/INDEX.md)
*   架构分册：[docs/architecture/](docs/architecture/)（app-products / color-rendering / composite / deliver-export / edit-timeline / shared-foundations）
*   编解码白皮书：[Topos Codec 白皮书](native/topos_codec/Topos_Codec_白皮书.md)（[English](native/topos_codec/Topos_Codec_Whitepaper.md)）· [Topos Image 白皮书](native/topos_codec/Topos_Image_白皮书.md)（[English](native/topos_codec/Topos_Image_Whitepaper.md)）
*   插件开发：[docs/PLUGIN_DEV_GUIDE.md](docs/PLUGIN_DEV_GUIDE.md)
*   架构决策记录：`docs/adr/`、`docs/image/`（ADR-I 系列）、`native/topos_codec/docs/`（ADR-C 系列）

## 5. 兼容性与环境

**本项目仅支持 Qt6（PyQt6 ≥ 6.10.2，requirements.txt 所列版本），不支持 Qt5**（QtWebEngine/Chromium 版本下限所致）。Python ≥ 3.10（CI 验证 3.10 / 3.12）。虚拟环境 (`.venv`) 不可跨 CPU 架构复制，迁移机器时须重建。

版本要求细节见 [docs/getstarted/installation.md](docs/getstarted/installation.md)；平台问题排查见 [docs/getstarted/faq.md](docs/getstarted/faq.md)。

## 项目信息

*   版本：**3.0**（`pyproject.toml`）
*   开源协议：**GPL-3.0**（完整文本见 [LICENSE](LICENSE)）
*   联系方式：[toposhub@outlook.com](mailto:toposhub@outlook.com)
*   测试规模：1400+ 测试文件 / 2.2 万+ 用例（近期战绩 `tests/edit` 8216/0、`composite` 5571/0 全绿）
*   界面语言：10 种（简体中文、英语、德语、西班牙语、法语、意大利语、日语、韩语、葡萄牙语（巴西）、俄语）
