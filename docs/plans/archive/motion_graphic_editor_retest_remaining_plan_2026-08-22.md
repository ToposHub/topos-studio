# Motion Graphic 编辑器二次真实测试与剩余优化计划

**审计日期：** 2026-08-22
**基线提交：** `d1d3a4ae test: align regression assertions with audit remediation changes`
**适用范围：** `src/features/teaching_animation`、`src/features/edit/core/render_cache.py`、MG 编辑器脚本与相关测试
**关联文档：**

- `docs/motion_graphic_web_editor_implementation_plan_2026-08-21.md`
- `docs/motion_graphic_web_editor_remaining_optimization_plan_2026-08-21.md`
- `docs/motion_graphic_editor_ui_ux_audit_plan_2026-08-21.md`
- `docs/mg_audit_remediation_2026-08-22.md`

## 1. 结论摘要

本轮不是只看代码，而是按“自动化回归 → Qt 用户路径 → 真实文件/资源 → WebEngine/浏览器路径 → 失败项复核”的顺序重新执行。结论如下：

1. MG 编辑器的核心数据模型、HTML/SVG 结构、模板编译、图层操作、关键帧、撤销/重做、缓存桥接、打包资源完整性已经达到可继续开发的状态。
2. 当前仍不能宣称“计划表全部完成”。至少有一个可直接复现的 UI 功能缺陷：素材库选中 HTML 模板后，详情、GIF 预览和插入按钮为空/禁用；模板卡片还会把开发机绝对路径暴露在 tooltip 中。
3. WebEngine 相关链路在当前测试环境无法稳定启动：粒子真实流程在页面加载处超时，DevTools Apply 流程的首个页面加载失败后又级联出 3 个无效错误。之前报告中“真实浏览器 6/6”是在另一运行环境得到的，本轮不能在当前环境复现，因此必须补充环境诊断和明确的能力报告。
4. 码率验收目前过于宽松，`verify_mg_cache_bitrate.py` 的默认模式只要码率大于 0 就能通过；在 1920×1080 实测中 high_quality 噪声样本达到约 79.8 Mbps，已经超出用户提出的 20–40 Mbps 目标。脚本的 `--help` 还会直接崩溃。
5. 全量 Composite 隔离回归仍有 5 个文件失败（共 179 个文件通过），包括 `motion_graphic` 空注册项、shader 契约不一致、拖拽数值行为回归以及 OpenGL offscreen 平台不支持导致的交互失败。
6. 模板“统一目录”的读侧基本完成，但用户可写目录、来源优先级、复制迁移、项目级模板持久化和模板包写入侧仍没有形成完整产品闭环；DevTools 的属性级 DOM→场景映射、`module.py` 的完整国际化、干净机器验收和 macOS 前台窗口实测也仍未完成。

因此下一阶段应优先修复 **N0/N1/N2/N3**，再处理功能性完善和视觉/桌面验收，而不是继续增加新的绘制工具。

## 2. 本轮真实测试证据

### 2.1 测试环境边界

本轮运行环境没有桌面显示服务器：

```text
DISPLAY=
WAYLAND_DISPLAY=
QT_QPA_PLATFORM=
```

Qt 测试主要使用 `QT_QPA_PLATFORM=offscreen`，WebEngine 使用 `--no-sandbox --disable-gpu` 作为兼容尝试。因此：

- Qt 控件、信号、模型、命令、HTML 字符串和离屏截图可以作为真实代码证据。
- macOS 原生窗口层级、应用切换后的前台关系、真实 OpenGL/`QOpenGLWidget`、真实 Chromium 页面加载不能在本环境视为通过。
- 不能把 offscreen 下的 WebEngine 超时直接等同于所有用户机器都会崩溃，但它已经证明当前 E2E 缺少可诊断的环境前置检查。

### 2.2 已通过的验证

| 验证范围 | 实际结果 | 结论 |
|---|---:|---|
| MG 核心/渲染/UI/编辑/模板/组件大回归 | **422 passed, 1 skipped** | 核心路径稳定 |
| `scripts/run_motion_graphic_validation.sh --focused` | **347 passed** | 现有专项门禁通过 |
| Teaching Animation + HTML 导入/注册/retime/组件 | **94 passed** | 相关功能回归通过 |
| 用户路径 `scripts/e2e_mg_user_journey.py --offscreen` | **16 passed, 0 failed, 2 skipped** | A 路径完整，真实 WebEngine 路径被环境跳过 |
| Preset Library Qt E2E | **7 passed** | 预设插入、拖拽、撤销/重做、ZIP、GIF 通过 |
| 打包资源检查 | **PASS** | 当前 wheel 内 13 presets、13 GIF、模板、locale 齐全 |
| Python `compileall` | **PASS** | 本轮涉及模块无语法错误 |
| 参数面板隐藏/恢复离屏探针 | **PASS** | 基本恢复逻辑可运行，仍需 macOS 手工验收 |

用户路径 A 中已经真实创建/编辑并验证了：文字、矩形、Frame、键盘输入、样式、DOM 标记、Flex、嵌套、Token/Theme、Keyframes、Undo/Redo、复制样式、缓存质量、两帧渲染和 roundtrip。

### 2.3 未通过、跳过或无法确认的验证

| 验证范围 | 实际结果 | 需要如何解读 |
|---|---:|---|
| `scripts/e2e_mg_particle_flow.py --offscreen` | **1 passed, 1 failed, 4 skipped** | 包加载通过；WebEngine 页面加载超时，后续粒子运行/导出无法确认 |
| `scripts/e2e_mg_devtools_apply.py` | **0 passed, 4 failed** | D1 页面加载超时；D2–D4 继续执行并产生级联错误，脚本本身缺少失败短路 |
| Composite 进程隔离回归 | **179 个文件通过，5 个文件失败** | 不能视为全绿门禁 |
| 五个失败 Composite 文件复跑 | **112 passed, 8 failed** | 失败点可稳定定位，见第 4 节 |
| 码率低分辨率非 strict | **6/6 passed** | 只说明 FFmpeg 产生了正码率，不代表达到目标 |
| 码率 320×180 strict | **5/6 passed** | high_quality 噪声约 13.6 Mbps，目标 40 Mbps 的比例约 0.339 |
| 码率 1920×1080 strict | **6/6 passed** | high_quality 噪声实测约 79.8 Mbps，超出 20–40 Mbps 目标 |
| `verify_mg_cache_bitrate.py --help` | **直接 TypeError 崩溃** | 验收工具本身有缺陷 |

## 3. 已确认的完成项与边界

以下项目可以标为“当前代码已覆盖”，但仍需保留相应的真实桌面验收：

- HTML/SVG/文本以 DOM/CSS 为主的编辑模型已建立，未退化为单纯位图。
- 统一只读包目录可以发现结构化预设和 HTML 模板，并生成拖拽 payload。
- 内置模板包包含 `manifest.json`、`template.html` 和 `preview.gif`，包资源能够进入 wheel。
- 图层、元素、样式、关键帧、页面、Token/Theme、Undo/Redo、复制粘贴样式、导入导出等核心命令已有覆盖。
- Preview 顶部工具栏、参数/图层等面板的基本布局和隐藏恢复已有实现。
- Effects 面板已不再依赖一组硬编码的中文 MG 条目；英文 locale 的核心标签测试通过。
- 组件生命周期和重新打开/缓存桥接有回归覆盖。

这些通过项不代表以下风险已经消失：WebEngine 页面是否能在真实 macOS 应用中稳定加载、窗口是否始终置前、模板能否由用户复制迁移、所有 AI 状态文案是否英文、实际码率是否处在产品目标范围内。

## 4. 未完成或仍存在 Bug 的清单

优先级说明：

- **P0**：阻断主要用户路径或会造成错误操作/错误发布。
- **P1**：重要功能缺口、跨环境不稳定或会影响正式发布。
- **P2**：质量、可维护性、诊断和体验问题。

### R-01（P0）素材库选中 HTML 模板后详情与插入功能失效

**证据**

通过真实 Qt 控件探针选择 HTML Templates 分类后得到：

```text
html_category_index= 7
grid_count= 1
item_text= Particle Flow Background
item_kind= html_template
insert_enabled= False
name_label=
preview_has_movie= False
tooltip= Cyan particle streams ...
[html_template] v1
/Users/testuser/.../preview.gif
```

**影响**

用户按照“模板支持 GIF 预览、双击/插入使用模板”的预期点击模板卡片时，看不到名称/元数据/GIF，也无法点 Insert。All Categories 视图把模板放入第一组时，默认选中项还可能就是一个禁用项。

**相关代码**

- `src/features/teaching_animation/ui/asset_library_panel.py`
  - `_on_selection_changed`
  - `_show_record`
  - `_insert_current`
  - `_append_template_items`
- `src/features/teaching_animation/core/mg_package_registry.py`
- `src/features/teaching_animation/ui/motion_graphic_editor_window.py::_apply_library_template`

**实施步骤**

1. 将详情模型从仅接受 `PresetRecord` 改为接受统一 `PackageRecord`/模板记录，至少包含 `kind`、`display_name`、`description`、`preview_path`、`version`、`origin` 和可用操作。
2. `_on_selection_changed` 根据卡片 `kind` 分支读取结构化预设或 HTML 模板，不再调用只接受 preset id 的 `library.get()`。
3. HTML 模板选中时显示 GIF `QMovie`、名称、版本、来源和描述；GIF 不存在时显示统一占位图和明确原因。
4. `_insert_current` 按 `kind` 发出 `presetActivated` 或 `templateActivated`，不能把 HTML 模板伪装成结构化 preset。
5. 对 all-categories 的默认选择采用第一个可插入项，或在详情为空时主动选择可用项。
6. tooltip 不再暴露绝对开发路径，只显示“Preview available/预览可用”或相对包内资源名。

**验收**

- 选中 `Particle Flow Background` 后，名称、描述、GIF 预览、版本和 Insert 均可见/可用。
- 单击 Insert、双击卡片、拖放到画布三种路径都能创建正确的 HTML layer。
- 结构化 preset 旧路径不回归。
- 新增测试：`tests/ui/test_mg_asset_library.py` 覆盖 HTML 选择详情；`scripts/e2e_mg_preset_library.py` 增加模板选择/插入段落。

### R-02（P0/P1）WebEngine 页面加载没有统一前置检查，导致 E2E 级联错误

**证据**

- 粒子脚本 P2 在 `timed out waiting for WebEngine page load` 失败，P3/P4/P6 因此跳过。
- DevTools 脚本 D1 同样页面加载超时；D2、D4 出现 `NoneType has no attribute page`，D3 报“applied layer lost DevTools edit”。这些不是 4 个独立功能失败，而是 D1 失败后脚本仍继续执行造成的级联。

**实施步骤**

1. 在两个 E2E 入口加入统一 `WebEngineReadiness`：检查 QtWebEngine 初始化、页面对象、`loadFinished`、console message、render process terminated 和首屏 DOM 标记。
2. D1/P2 失败后立即将后续依赖步骤标记为 `SKIP (blocked by browser readiness)`，禁止产生误导性 `NoneType` 失败。
3. 输出完整诊断：Qt/QtWebEngine 版本、平台、`QT_QPA_PLATFORM`、Chromium flags、页面 URL、最后一个 console message、load error、render process 状态。
4. 提供 `--require-browser` 与 `--allow-skip-browser` 两种模式：CI 用前者，开发机诊断可用后者。
5. 对加载超时增加一次受控重试和更短的失败截图/HTML dump，不使用无限等待。

**验收**

- 浏览器不可用时报告 1 个根因 + N 个 blocked skip，不出现 `NoneType` 级联错误。
- 浏览器可用时，粒子脚本、DevTools 脚本都能继续到真实 DOM/导出断言。
- CI 能明确区分“浏览器环境不可用”和“功能断言失败”。

### R-03（P1）粒子真实运行与 DevTools Apply 在当前环境没有得到可重复证据

这不是在没有桌面环境时武断判定应用逻辑一定错误，而是发布门禁仍缺真实证据。现有报告中曾记录另一环境的真实浏览器通过，本轮当前环境无法复现，说明缺少可迁移的运行前置与诊断。

**实施步骤**

1. 记录一份受支持的 macOS 测试矩阵：macOS 版本、GPU、Qt、QtWebEngine、Python、Chromium flags。
2. 增加无头 smoke 与真实窗口 smoke 两个目标，禁止把 offscreen 结果冒充真实桌面结果。
3. 粒子路径必须验证：GIF 预览、模板插入、粒子持续运动、绘制 SVG/HTML 元素后粒子仍更新、导出两帧不相同。
4. DevTools 路径必须验证：打开 DevTools、修改 DOM/CSS、Apply、Source/Preview 同步、Discard、重新打开不丢更改。
5. 将上述证据输出到带环境信息的 JSON/PNG 工件目录。

**验收**

- 受支持 macOS 环境连续运行 3 次无随机页面加载失败。
- 粒子导出两帧差异超过阈值，且新增 SVG/text layer 不会冻结背景动画。
- DevTools Apply 后 DOM 和场景模型一致，关闭/重开窗口仍可恢复。

### R-04（P1）码率验收规则与实际产品目标不一致，验收工具本身会崩溃

**证据**

- `verify_mg_cache_bitrate.py --help` 抛出 `TypeError: %o format: an integer is required, not dict`，工具无法正常显示帮助。
- 320×180 strict：high_quality 噪声约 13.6 Mbps，只达到目标 40 Mbps 的约 0.339。
- 1920×1080 strict：high_quality 噪声约 79.8 Mbps，高于用户希望的 20–40 Mbps 上限。
- 默认非 strict 只检查 `avg_bitrate_kbps > 0`，所以“有码率”会被误报成“达到目标”。

**实施步骤**

1. 修复 argparse docstring 的 `%` 插值问题：不要把含 `%` 的 `__doc__` 直接作为旧式格式化模板，改用安全字符串或转义百分号。
2. 明确每个质量档的产品策略：建议以 1080p 复杂场景平均 20–40 Mbps 为主目标，并定义可接受上下限/容差，而非只设置一个参考值。
3. 将“目标范围”“实测平均值”“峰值/文件大小”“是否因低复杂度低于下限”分开报告。
4. 对不同分辨率使用可解释的缩放公式，同时对 WebM/FFmpeg padding 造成的低复杂度最低码率单独标记，不让 padding 伪装成画质达标。
5. 将 strict 作为 CI 默认门禁；另提供显式 `--report-only` 供探索性测试。
6. 增加真实 10 秒、24 fps、1920×1080 的噪声/渐变/粒子样本，避免只用 1 秒低分辨率样本。

**验收**

- `--help` 返回 0，且显示完整用法。
- 目标范围外的 high_quality 79.8 Mbps 实测必须失败或明确标记超上限。
- 20–40 Mbps 目标内的样本通过；低复杂度样本显示“内容复杂度不足/仅作编码可用性通过”，不能写成“目标码率通过”。
- CI 报告包含每个档位的真实 Mbps、目标范围、差值和文件路径。

### R-05（P1）模板统一目录只有读侧，用户可写目录与迁移闭环未完成

**证据**

`default_motion_graphic_template_root()` 当前返回源码/包内的：

```text
src/features/teaching_animation/templates/motion_graphic
```

发现逻辑只扫描该目录的直接子目录，编辑器的“打开模板库”也打开该包路径。安装后的用户不应通过修改 Python 包目录来复制模板。会话发布模板也没有稳定的 preview GIF 路径。

**实施步骤**

1. 定义三层来源：bundled、user、project，分别映射到应用资源目录、用户数据目录、项目内 `.topos/motion_graphic_templates`。
2. 为每个包记录 `origin`、绝对路径（内部使用）、稳定 id、版本和冲突策略。
3. 实现“打开用户模板目录”“从文件夹导入”“导出模板包”“导出带预览的 ZIP”“复制到项目模板库”。
4. 导入时校验 manifest、template.html、preview.gif/静态占位图、参数 schema 和安全路径；错误包进入可解释的失败列表。
5. 保存自定义模板时自动生成 preview GIF 或明确要求用户提供 preview 资源，禁止生成没有预览但在 Library 中显示可预览的假状态。
6. 记录迁移版本，保证未来目录结构变化可以升级。

**验收**

- 用户无需修改源码目录即可复制一个文件夹并在重启后发现模板。
- user 模板覆盖 bundled 同 id 时有明确优先级和 UI 来源标识。
- project 模板随项目保存、换机器可迁移，路径不泄露到 tooltip/日志之外。
- 缺少预览、manifest 或参数时有可操作错误提示。

### R-06（P1）统一包目录的写路径仍分散，预设与 HTML 模板操作不对称

当前 `MgPackageCatalog` 是只读聚合视图，写操作仍由不同系统负责；Library 的 ZIP 保存/导入主要覆盖结构化 preset，HTML template 尚未完全复用同一套包协议。

**实施步骤**

1. 抽象 `PackageStore` 接口：`list/read/validate/write/import/export/remove`。
2. 让 preset、HTML template、未来 SVG/组件模板都实现同一 manifest/preview/thumbnail/schema 协议。
3. Save as Preset、Save as HTML Template、Import ZIP、Export ZIP、Duplicate、Delete 全部通过 `PackageStore`，保留 owning system 的业务回调。
4. 任何写入都通过临时目录 + 原子 rename，失败时不留下半包。
5. 为每种 package kind 添加权限/来源限制，bundled 只读，user/project 可写。

**验收**

- Library 中两类包的保存、导入、导出、删除、重启发现行为一致。
- 包损坏、重复 id、非法路径均有单元测试和 UI 错误提示。
- 不破坏当前 13 个内置 preset 和 1 个 HTML 模板的打包内容。

### R-07（P1）DevTools 仍缺属性级 DOM→场景映射与明确的草稿所有权

现有 Apply 能力的回归主要验证整体 HTML/Source 变化；计划中仍未完成 `SetElementProperty` 级命令、Source 与 DevTools 草稿冲突规则和窗口级 Apply/Discard 状态。

**实施步骤**

1. 为可编辑 DOM 属性建立白名单映射：text、x/y、width/height、fill/color、opacity、font、transform、display 等。
2. DevTools 产生 DOM diff 后优先生成属性级命令；无法映射的修改才回退为整体 HTML replacement，并显示回退原因。
3. 引入单一 draft owner：Source、DevTools、外部 HTML 只能有一个活动草稿，另一个必须显示“有未应用更改”。
4. 提供 Apply、Discard、Reload 三态，明确哪些更改已经进入 undo stack。
5. 将更新后的 layer id/data-element-id 保持稳定，避免编辑后所有元素被当成新图层。

**验收**

- 修改一个元素的颜色只生成一个属性命令，其他元素和动画不被重建。
- Apply→Undo/Redo、Discard、关闭/重开均符合预期。
- 未映射 CSS 属性不会静默丢失，UI 会显示回退提示。

### R-08（P1）模块级 AI/status 文案仍未完整国际化

核心 tab、Marker、Page 等英文 locale 测试通过，但 `src/features/teaching_animation/module.py` 仍包含大量中文 UI、状态、提示和 AI prompt 字符串。现有无硬编码检查只覆盖 `edit_effects_panel.py`，不能代表整个 MG 编辑器英文模式完整。

**实施步骤**

1. 扫描 `src/features/teaching_animation` 全目录，建立字符串清单并按 UI、日志、AI prompt、错误、开发诊断分类。
2. 用户可见字符串全部迁移到 locale key；日志和 prompt 分开，避免把面向模型的中文 prompt 当 UI 文案。
3. 为 key 提供 `zh_CN` 和 `en_US`，缺失 key 采用可观测 fallback 并在开发模式告警。
4. 添加英文模式截图/文本 smoke：顶部菜单、参数、图层、页面、Source、DevTools、素材库、保存/导入错误、Marker、空状态。
5. 检查动态字符串拼接、复数、数字/时间单位和 tooltip，不能只检查静态中文字符。

**验收**

- 英文模式下 MG 编辑器所有用户可见文案无意外中文。
- 中文模式不回归；locale 缺失时不会显示 key 原文或空白按钮。
- 新增全目录 localization lint 与 UI smoke。

### R-09（P0/P1）Composite 仍有确定性失败：空节点注册项与 shader 契约不一致

五个失败文件中，以下两项不依赖真实桌面即可稳定复现：

1. `tests/composite/test_composite_node_contract.py::TestRegistryCompleteness::test_registry_has_no_null_entries`
   - `NODE_REGISTRY` 中 `motion_graphic` 值为 `None`。
2. `tests/composite/test_composite_nodes_merge.py::TestBBoxMapping::test_sample_layer_shader_compiles`
   - 测试期待 `_BLEND_FRAGMENT` 包含 `u_output_bbox`，当前 shader 契约不一致。

**实施步骤**

1. 明确 `motion_graphic` 节点是可用节点、延迟注册节点还是禁用节点；生产 registry 不允许无说明的 null entry。
2. 若节点尚未支持，移出生产 registry 并在 capability registry 中声明；若支持，补齐实现、工厂和生命周期释放。
3. 统一 merge shader 的 uniform 命名、bbox 坐标语义和测试 fixture；不要只改测试字符串。
4. 为 shader contract 增加编译 smoke 和 uniform reflection 检查。

**验收**

- Composite 进程隔离脚本达到 184/184 文件通过，或明确把平台专属测试隔离并显示原因。
- `motion_graphic` 节点在注册、创建、渲染、释放四个阶段都有测试。
- shader 代码、Python uniform 上传和测试 fixture 使用同一份契约。

### R-10（P1/P2）Composite 交互/拖拽数值存在回归或平台依赖

复跑失败包括：

- `test_composite_transform_move.py::test_body_press_starts_virtual_move_drag`：offscreen 平台提示 `QOpenGLWidget is not supported on this platform`，`handled` 为 false。
- `tests/composite/test_drag_value_widget.py` 五项失败：默认/Shift/Ctrl 拖拽的期望值与实际值不一致（例如期望 52.0 实际 83.3333，期望 525.0 实际 916.6667）。

**实施步骤**

1. 先确认 DragValue 的产品契约：普通拖拽、Shift 粗调、Ctrl 细调、范围归一化、最小步长分别应是什么。
2. 统一坐标到数值的换算，不要让 widget 宽度、DPR 或 offscreen 坐标改变语义。
3. 为交互公式增加纯函数测试，再用真实 widget 做少量端到端测试。
4. 对 OpenGL 依赖测试加入能力探测；offscreen 不支持时应 skip 并给出原因，真实 macOS OpenGL 测试必须在发布矩阵中执行。
5. 修复后重新跑五个失败文件及全量隔离 Composite。

**验收**

- 同一输入轨迹在 macOS、offscreen（可测试部分）和不同 DPR 下得到同一数值。
- 普通/Shift/Ctrl 手势的边界、0.1 最小值、负值和大范围均有断言。
- 平台不支持 OpenGL 时不产生误导性失败，但 release gate 不得因此把真实 OpenGL 测试删掉。

### R-11（P1）干净机器/安装后完整流程仍未闭环

当前 wheel 资源检查通过，但它只验证包内文件和有限的绝对路径扫描，不能证明新用户在无源码目录、无开发缓存、无旧配置的机器上能完成：启动 → 打开模板 → 编辑 → 预览 → 缓存 → 导出 → 重启恢复。

**实施步骤**

1. 创建临时虚拟环境/临时用户数据目录，安装 wheel，不引用源码路径。
2. 运行资源发现、locale、模板 Library、GIF 预览、HTML layer、缓存和导出 smoke。
3. 清空/伪造缺少模型 manifest、离线 asset、旧模板目录，验证启动只告警不崩溃。
4. 记录完整路径来源，确认没有写入源码包目录。
5. 将干净机器流程纳入 release candidate 检查。

**验收**

- 新环境不需要 IDE、Playwright、FFmpeg 开发路径即可完成核心静态流程。
- 所有用户可写资源落在用户/项目目录，不会污染 wheel 安装目录。
- 缺失可选 AI manifest 不会导致 MG 编辑器崩溃或窗口消失。

### R-12（P2）打包资源绝对路径扫描存在运算符优先级缺陷

`scripts/verify_packaged_resources.py` 中类似以下条件：

```python
if name.endswith((".json", ".html", ".md", ".py", ".txt")) and "/presets/" in name or "/templates/" in name:
```

由于 `and` 高于 `or`，模板目录下所有文件都会进入扫描，而 `locales` 等目录的文本资源可能不被检查。当前检查通过不代表扫描覆盖完整。

**实施步骤**

1. 加括号，明确“文本扩展名且属于资源目录”的逻辑。
2. 对 locales、manifest、模板、preset、脚本资源分别测试绝对路径泄露。
3. 二进制 GIF/ZIP 不进行文本解码扫描，改用白名单和 MIME/扩展名处理。
4. 为正例、反例增加单元测试。

**验收**

- 人为在 locale/template/preset 中放入绝对路径时都能失败。
- 正常 wheel 检查仍通过，且不会因二进制资源误报。

### R-13（P1/P2）macOS 窗口置前、应用切换和视觉布局仍缺真实手工验收

代码当前使用 `Qt.Tool` 与 `WindowStaysOnTopHint`，离屏探针能隐藏/恢复右侧面板，但用户曾多次反馈“切换应用回来后 MG 编辑器跑到项目后面”。本轮没有真实 macOS Window Server，因此不能宣称已修复。

**实施步骤**

1. 在真实 macOS 上测试：项目窗口 → MG 编辑器 → Safari/VS Code → 返回项目 → 返回 MG；重复 10 次。
2. 测试编辑器最小化、全屏、Mission Control、外接屏、关闭右侧面板、重新打开参数面板。
3. 统一窗口激活策略：打开/显示时 `raise_()`、`activateWindow()`，必要时使用 macOS 原生激活 API；避免无条件全局置顶干扰其他应用。
4. 增加明确的“MG Editor”窗口标题/模式标识和焦点恢复日志。
5. 建立宽度较窄时 tabs、Preview 信息、DevTools 按钮、底部播放控制的视觉截图基线。

**验收**

- 10 次应用切换后 MG 编辑器仍按产品规则置前/可从菜单恢复。
- 参数面板隐藏后可从菜单或右侧按钮恢复，状态不丢。
- 中文/英文、窄窗口/宽窗口下没有省略为“...”而看不懂的关键标签。

## 5. 推荐实施顺序

### N0：先修测试基础设施与能力诊断

- 完成 R-02：WebEngine readiness、失败短路、环境诊断。
- 修复 R-04 的 `--help` 崩溃，建立 strict/report-only 两种模式。
- 给 Composite 平台测试加 capability 标识，避免把 OpenGL 缺失与业务错误混在一起。

**完成标志：** 每次失败都能归类为“环境不可用、测试脚本问题、确定性代码失败、真实产品失败”四类之一。

### N1：修复素材库模板卡片主路径

- 完成 R-01，并把模板选择/预览/插入加入真实 Qt E2E。
- 补齐 tooltip 路径脱敏。

**完成标志：** 用户从 Effects/Library 看到模板后，单击、双击、拖拽都能得到同一 HTML layer。

### N2：锁定码率产品契约

- 完成 R-04，使用 1080p/24fps/10 秒真实样本。
- 把 20–40 Mbps 作为明确目标范围或由产品重新确认每档范围。

**完成标志：** 验收报告不再把“正码率”写成“达到目标”，超上限会失败。

### N3：完成模板写入、迁移和包协议

- 完成 R-05、R-06。
- 明确 bundled/user/project 三层来源和冲突策略。

**完成标志：** 用户复制一个模板文件夹到用户/项目目录，重启后可发现、预览、插入、导出和迁移。

### N4：完成 DevTools 生产级同步

- 完成 R-07，并在浏览器可用机器跑真实 E2E。

### N5：完成国际化与窗口/视觉验收

- 完成 R-08、R-13。
- 生成中英文窄/宽窗口截图基线。

### N6：清理 Composite 与发布门禁

- 完成 R-09、R-10、R-11、R-12。
- 重新执行全量进程隔离测试和干净机器安装测试。

## 6. 建议新增/修改的测试清单

### 自动化测试

- `tests/ui/test_mg_asset_library.py`
  - HTML template selection renders details and GIF.
  - HTML template Insert emits `templateActivated`.
  - all-categories default selection chooses an actionable card.
  - tooltip contains no absolute development path.
- `tests/ui/test_mg_package_catalog.py`
  - bundled/user/project origin and conflict precedence.
- `tests/render/test_mg_browser_readiness.py`
  - page load success, timeout, render-process crash and console error produce distinct outcomes.
- `tests/render/test_bitrate_policy.py`
  - target range, low-complexity warning, high-quality upper bound and report-only mode.
- `tests/composite/test_node_registry_contract.py`
  - no unexplained null entries.
- `tests/composite/test_shader_uniform_contract.py`
  - Python upload names and GLSL declarations agree.
- `tests/composite/test_drag_value_math.py`
  - pure gesture conversion for default/Shift/Ctrl/DPR/range boundaries.
- `tests/core/test_template_store.py`
  - import/export/copy/migrate/atomic write/corrupt package.
- `tests/core/test_full_teaching_animation_localization.py`
  - full module tree inventory and locale completeness.

### 真实 Qt/WebEngine/macOS 测试

- `scripts/e2e_mg_user_journey.py --require-browser`
- `scripts/e2e_mg_particle_flow.py --require-browser`
- `scripts/e2e_mg_devtools_apply.py --require-browser`
- `scripts/e2e_mg_preset_library.py --include-html-templates`
- `scripts/verify_mg_cache_bitrate.py --strict --width 1920 --height 1080 --seconds 10 --fps 24`
- `scripts/verify_clean_install.py`（建议新增）
- macOS 手工窗口焦点与视觉截图清单（建议新增 `docs/mg_macos_manual_qa_checklist.md`）

## 7. 本轮复测命令记录

以下命令是本计划的证据来源，修复后应按同样命令重跑并保留结果：

```bash
./.venv/bin/python -m pytest -q \
  tests/render/test_scene_renderer_router.py \
  tests/render/test_html_s0_end_to_end.py \
  tests/render/test_motion_graphic_vector_shapes.py \
  tests/render/test_html_export_e2e.py \
  tests/render/html_layer \
  tests/ui/test_motion_graphic_manual_controls.py \
  tests/ui/test_editor_html_preview.py \
  tests/ui/test_editor_html_templates.py \
  tests/ui/test_html_inspector.py \
  tests/edit/test_motion_graphic_manual_editing.py \
  tests/edit/test_motion_graphic_editor_cache_bridge.py \
  tests/core/test_tm04_full_templates.py \
  tests/core/test_w3_templates.py \
  tests/render/test_motion_graphic_reopen_gate.py \
  tests/render/test_motion_graphic_gpu_gate.py \
  tests/render/test_bitrate_probe.py \
  tests/core/test_component_lifecycle.py \
  tests/core/test_components.py \
  tests/ui/test_mg_asset_library.py \
  tests/ui/test_mg_package_catalog.py \
  tests/ui/test_mg_user_journey_regressions.py \
  tests/ui/test_devtools_ownership_contract.py \
  tests/ai/test_motion_graphic_integration.py

./.venv/bin/python scripts/e2e_mg_user_journey.py --offscreen
./.venv/bin/python scripts/e2e_mg_preset_library.py --offscreen
./.venv/bin/python scripts/e2e_mg_particle_flow.py --offscreen
QT_QPA_PLATFORM=offscreen QTWEBENGINE_CHROMIUM_FLAGS='--no-sandbox --disable-gpu' \
  ./.venv/bin/python scripts/e2e_mg_devtools_apply.py
VALIDATION_PYTHON=./.venv/bin/python scripts/run_motion_graphic_validation.sh --focused
scripts/run_composite_tests.sh
./.venv/bin/python scripts/verify_packaged_resources.py
./.venv/bin/python scripts/verify_mg_cache_bitrate.py --help
./.venv/bin/python scripts/verify_mg_cache_bitrate.py \
  --width 1920 --height 1080 --seconds 1 --fps 24 --strict
```

## 8. macOS 手工验收脚本

在有真实桌面和 Chromium/FFmpeg 的 macOS 机器执行，每一步记录截图或录屏：

1. 启动项目并打开一个包含视频和 MG layer 的项目。
2. 双击 MG layer，确认编辑器位于项目窗口前方，标题和 Preview 正常。
3. 在 Parameters、Layers、Scenes、Source、DevTools 间切换，确认标签不被压缩成不可读的省略号。
4. 隐藏右侧面板，点击菜单栏下方“参数面板/显示面板”重新打开；重复 5 次。
5. 在 Layers 中选择 HTML、SVG、文字、Frame，确认 Parameters 随选择更新。
6. 选择素材库中的 HTML 模板，确认 GIF hover 播放、详情和 Insert 可用。
7. 拖入模板并绘制矩形/文字/SVG，确认背景粒子继续运动。
8. 打开 DevTools，修改一个元素的文字和颜色，Apply；再 Undo/Redo、Discard、重开编辑器。
9. 按空格播放/暂停，使用上一页/下一页、Marker、循环和时间码控制。
10. 调整参数时切换到 Safari/VS Code，再切回项目和 MG 编辑器，确认窗口焦点按规则恢复。
11. 用中文、英文各执行一次，检查所有用户可见文案、按钮和错误提示。
12. 导出缓存，记录分辨率、时长、文件大小和 ffprobe 平均码率；确认复杂粒子样本处在目标范围。
13. 关闭并重启项目，确认模板来源、页面、图层和参数恢复，且没有写入源码包目录。

## 9. 完成定义（Definition of Done）

本计划只有在以下条件同时满足时，才能把“MG 优化计划已完成”标记为完成：

- [ ] R-01 到 R-04 的 P0/P1 阻断项已修复，并有自动化回归。
- [ ] WebEngine/DevTools/粒子路径在受支持 macOS 真实环境连续 3 次通过。
- [ ] 码率 strict 门禁使用产品目标范围，工具 `--help` 和报告均正常。
- [ ] 模板可由用户复制文件夹迁移，bundled/user/project 来源和冲突规则清晰。
- [ ] Composite 失败项要么修复并全绿，要么以明确 capability skip 进入发布报告，不能静默忽略。
- [ ] 全目录中英文 UI 文案完成 locale 覆盖。
- [ ] 干净 wheel 安装完成完整核心流程，不依赖源码目录或开发缓存。
- [ ] macOS 窗口置前、应用切换、窄窗口布局、参数面板恢复和视觉截图完成手工验收。
- [ ] 新旧计划表、测试工件和失败原因均链接到版本提交，便于下一轮复测。

## 10. 当前建议

现在不要把“422 个专项测试通过”当作最终完成信号。最先修复素材库 HTML 模板卡片（R-01）、WebEngine readiness/测试短路（R-02）和码率验收契约（R-04），因为这三项分别对应用户直接可见的功能阻断、当前无法定位真实浏览器问题、以及已经被实测证明与 20–40 Mbps 目标不一致。随后完成模板用户目录与包写入闭环，再进入 DevTools 属性级同步、国际化和 macOS 前台窗口验收。
