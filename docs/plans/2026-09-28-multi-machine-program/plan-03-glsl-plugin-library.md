# 计划 03：GLSL 插件库扩充（Plugin Library）

分支：`plan/03-plugins` ｜ 性质：写插件（全部为新文件） ｜ 写路径：`plugins/*.tglsl`（仅新增）、`tests/plugins/`（新增目录）、`docs/plugins/2026-09-28/`、`src/shared/i18n/locales/*`（仅新增 `plugins.*` 命名空间键）、`docs/PLUGIN_DEV_GUIDE.md`（仅可追加新示例章节）

## 1. 目标

当前仓库只有 5 个 `.tglsl` DCTL 插件 + 1 个 `.tai` AI 插件，对调色软件来说太少。本计划按官方规范批量开发一批高质量着色器插件，每个插件走完整流程：规格 → 实现 → 验证 → 文档卡片 → i18n。**不改任何现有插件文件，不改 src 逻辑。**

## 2. 仓库关键事实（已核实）

- **插件自动发现**：`src/features/color/core/plugin_manager.py` 的 `PluginManager.scan_plugins(directory="plugins")` 会扫描 `plugins/*.tglsl`（DCTL）与 `plugins/*.tai`（AI），按 `@name` 等元数据解析注册——**新增 .tglsl 文件无需改任何 src 即可被加载**。
- **规范文档**：`docs/PLUGIN_DEV_GUIDE.md`（658 行），要点：
  - `@param` 定义 UI（slider / checkbox / color / select 四种控件）；
  - Capability Manifest 能力声明（P1-COLOR-15）；
  - 内置 uniforms 环境（分辨率、帧信息等，见文档第 3 节）；
  - 变量隔离机制（namespacing，动态注入变量必须命名隔离）；
  - 现成案例：电影感暗角、色差分离。
- **`.clinerules` GLSL 铁律**：浮点语境显式 `.0`；整数仅用于有意的整数语义；循环上限为编译期常量；不得无理由 `clamp(..., 0.0, 1.0)`（HDR 内部）；采样 UV 安全；性能敏感（4K 实时）。
- **现有 5 个 .tglsl**（作为风格与质量基准，先通读）：`warm_tint.tglsl`、`Film_Halation.tglsl`、`HighPass_Retouch.tglsl`、`bilateral_skin_smooth.tglsl`、`Color_Space_Transform.tglsl`。
- **i18n**：插件名/参数标签经 `I18N.tr("plugins.<id>.name")` 查找，locale 文件在 `src/shared/i18n/locales/`。

## 3. 插件候选清单（第一批 12 个，可按机器速度增减）

按实现难度从低到高排列；每个都是独立批次：

| # | 插件名（建议） | 说明 | 关键参数思路 |
|---|---------------|------|-------------|
| 1 | Film_Grain | 胶片颗粒（hash 噪声，按亮度加权） | 强度、颗粒尺寸、阴影/高光偏置 |
| 2 | Soft_Glow_Bloom | 阈值式柔光/泛光 | 阈值、半径（常量循环上限）、混合 |
| 3 | Halation_Extended | 在现有 Film_Halation 思路上的变体（不同核+色调） | 不得复制旧文件，独立实现 |
| 4 | Three_Way_Tint | 阴影/中间调/高光三段着色 | 3 个 color + 3 个强度 |
| 5 | Bleach_Bypass | 漂白旁路风格 | 强度、对比、饱和保留 |
| 6 | Radial_Blur_Pro | 径向/变焦模糊（固定 N 次采样的常量循环） | 采样数 select（每档对应常量）、强度 |
| 7 | Prism_Aberration | 升级版色散：径向 RGB 分离 + 旋转 | 分离量、角度、边缘衰减 |
| 8 | Vignette_Shapes | 多形状暗角（椭圆/矩形/羽化曲线） | 形状 select、羽化、亮度补偿 |
| 9 | Day_Night_Converter | 日景转夜景启发式 | 色温偏移、曝光、天空保护 |
| 10 | Cross_Process | 交叉冲洗风格曲线族 | 预设 select + 强度 |
| 11 | Skin_Even_Matte | 哑光肤色平滑（与 bilateral 不同的算法路径） | 半径、阈值、纹理保留 |
| 12 | Fake_Infrared | 假红外/通道交换艺术效果 | 通道映射 select、 glow 强度 |

规则：**禁止**在名称与文案中使用真实胶片/品牌商标名；风格模拟一律用通用描述名。

## 4. 单插件执行流程（每个插件 = 一个批次 = 一个子智能体）

1. **规格**（写入 `docs/plugins/2026-09-28/specs/<PluginName>.md`）：用途、参数表（名/类型/范围/默认/i18n key）、能力声明（预览/导出后端支持）、已知限制。
2. **实现** `plugins/<PluginName>.tglsl`：
   - 严格遵循第 2 节规范与 `.clinerules` GLSL 铁律；
   - 所有 `@param` 完整（含 select 的选项表）；文件头元数据齐全；
   - 性能预算：避免每像素昂贵运算（`pow`/`exp` 尽量查表或低次近似；循环上限必须是编译期常量）。
3. **验证**（按机器能力二选一或都做）：
   - **有 GPU**：用 ModernGL standalone context 写一个临时脚本（放 `docs/plugins/2026-09-28/tools/`，不进 src/）真实编译每个插件 shader，贴出编译输出到报告；
   - **无 GPU**：跑既有元数据解析路径（`PluginManager.scan_plugins` 的解析逻辑是纯 Python，可 headless 调用）确认每个文件解析成功、参数 schema 完整。
4. **测试**：在 `tests/plugins/test_plugin_batch1.py` 新增纯解析测试（无 GPU 依赖）：对每个新 .tglsl 断言——文件能被扫描解析、必需元数据字段存在、参数范围合法（min<max）、无重复 plugin_id、GLSL 源内无裸数字浮点（用正则抽查 `.0` 规则的可检测子集）。
5. **i18n**：向 `src/shared/i18n/locales/` 各语言文件**追加**本插件命名空间的新键（`plugins.<id>.name`、`plugins.<id>.params.*`、`plugins.<id>.desc.*`）。只增键，不动既有键。
6. **文档卡片**：`docs/plugins/2026-09-28/cards/<PluginName>.md`——前后对比示意说明（文字描述即可）、参数说明、典型用途、性能备注。
7. 更新 `PROGRESS.md`，`git commit -m "[plan-03] plugin: <Name> (batch N)"`。

## 5. 阶段划分

| 阶段 | 内容 | 验收 |
|------|------|------|
| P0 | 建目录结构；通读 PLUGIN_DEV_GUIDE + 5 个现有插件 + plugin_manager 解析代码，写 `STYLE_NOTES.md`（总结本仓库插件的质量基准与惯用写法） | 笔记覆盖元数据、namespacing、性能三类惯例 |
| P1–P3 | 按第 3 节清单逐批开发（每批一个子智能体，主会话抽查 GLSL 铁律合规性再合入） | 每插件 7 步流程全过 |
| P4 | 全量回归：headless 解析测试 + （有 GPU 则）全插件编译验证；补 `tests/plugins/test_plugin_registry.py`（全部插件 id 唯一、目录整洁） | 测试全绿 |
| P5 | `SUMMARY.md`（插件总表 + 效果索引）；把 2–3 个代表性插件作为新案例**追加**到 PLUGIN_DEV_GUIDE.md 的案例章节（仅追加，不改既有内容） | — |
| P6 | **收口**：把 `HANDOFF.md` 项、验证中发现但按边界未修的插件缺陷、规范/文档缺口，对账后结构化为 `FINDINGS.json`（id 前缀 `PLUG-`，schema 见总纲第七节） | `FINDINGS.json` | 条目数与 `HANDOFF.md` 等来源对账一致 |

## 6. 边界与禁止

- 不修改任何**既有** `.tglsl`/`.tai` 文件；不动 `plugin_manager.py` 及一切 src 逻辑。
- `src/shared/i18n/locales/` 仅允许追加 `plugins.*` 新键；**不要**动其他命名空间（那是 04 计划的领域）。
- 临时验证脚本一律放 `docs/plugins/2026-09-28/tools/`，不污染仓库根目录或 src/。
- 效果注册走自动发现；如果某插件需要 Effect Catalog 层面的额外接线（src 改动），**不做**，写 `HANDOFF.md` 说明所需 src 变更，留待主线。

## 7. 预估 token 消耗

12 插件 × (规格+实现+验证循环+文档+i18n)，含返工，一轮约 **3000 万 – 8000 万 token**；第二批（后续轮次）可再做 12–20 个。

## 8. 启动提示词（在本机器的 ZCode 新会话中粘贴）

```text
你是计划 03（GLSL 插件库扩充）的执行者。请先完整阅读以下文件并严格遵守：
1. docs/plans/2026-09-28-multi-machine-program/README.md（全局铁律）
2. docs/plans/2026-09-28-multi-machine-program/plan-03-glsl-plugin-library.md（本计划全文）
3. docs/PLUGIN_DEV_GUIDE.md（插件规范）
4. .clinerules（GLSL 铁律：浮点 .0、常量循环边界、HDR 不乱 clamp 等）

然后从阶段 P0 开始执行。要求：
- 确认当前分支是 plan/03-plugins，不是就先创建并切换。
- 严格按计划第 4 节的 7 步流程逐插件推进，一个插件一个 commit（前缀 [plan-03]）。
- 不改既有插件与任何 src 逻辑；i18n 只追加 plugins.* 新键。
- 每完成一个插件更新 PROGRESS.md；每批完成后主会话必须按 .clinerules 抽查 GLSL 合规性再算完成。
```
