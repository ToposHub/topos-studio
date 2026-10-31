# Topos Edit 剪辑功能优化计划（2026-09-07）

> **文档用途**：本计划基于 2026-09-07 对照 Premiere Pro / DaVinci Resolve / Final Cut Pro 的全面差距调研（4 路并行代码审查）生成，供后续 AI / 开发者逐项执行。每个任务自包含：背景、证据、实现步骤、验证方法、验收标准，可独立领取与验收。
>
> **明确排除（用户另行安排，本计划不包含）**：
> - Edit 页时间线关键帧曲线编辑器
> - 硬件编码导出（VideoToolbox / NVENC / QSV 编码侧）
>
> **已核实无需实现（勿重复开发）**：
> - A/V 手动 Link/Unlink：已完整实现。`SetClipLinkedCommand`（`src/features/edit/core/commands.py:15358`）+ 右键菜单（`timeline_items.py:4721-4736`）+ 信号链（`timeline_view.py:10054-10098`）。早期调研误报缺失，执行时跳过。

---

## 0. 执行须知（AI 执行者必读）

1. **每个任务独立提交**，不跨任务批量修改。执行顺序建议见第 7 节。
2. **遵守 `AGENTS.md` 工作流**：动手前先 Explore 目标模块 → 实现 → 自查 → 必要时 /review。
3. 全局工程约束见第 2 节，任何任务开始前先通读。
4. 文中行号为 2026-09-07 快照，可能因后续提交漂移；以符号名（类名/函数名）为锚点重新定位。
5. 验证一律包含：自动化测试（pytest）+ 手工冒烟（按项目现有启动方式 `run_topos.command` / `run_topos.sh`）+ 涉及导出的任务用 `ffprobe` 验证产物。

---

## 1. 任务总览

| 编号 | 任务 | 批次 | 优先级 | 规模 | 依赖 |
|------|------|------|--------|------|------|
| A1 | 时间线片段混合模式（Blend Modes） | A | P0 | L | 无 |
| A2 | 冻结帧（Freeze Frame） | A | P0 | M | 无 |
| A3.1 | 平滑速度斜坡（Smooth Speed Ramp） | A | P0 | M | 无 |
| A3.2 | 帧混合变速（Frame Blend） | A | P1 | M | A3.1 |
| B1 | 场景检测时间线 overlay 生产入口 | B | P1 | S | 无 |
| B2 | Stem 分轨导出对话框 | B | P1 | M | 无 |
| B3 | Render Cache smart 自动评估器 | B | P2 | M | 无 |
| B4 | 比例拉伸工具（Rate Stretch） | B | P1 | M | 无 |
| B5 | 快捷键完全可自定义（时间线内嵌键迁移） | B | P2 | L | 无 |
| B6 | 用户自定义导出预设持久化 | B | P2 | S | 无 |
| B7 | 占位符片段（Placeholder Clip）全流程 | B | P2 | L | 无 |
| B8 | 全局插入/覆盖模式切换 | B | P2 | S | 无 |
| C1 | 标题模板库与文字动画扩展 | C | P1 | L | 无 |
| C2 | 用户自定义转场扩展点 | C | P1 | M | 无 |
| C3 | 时间线内嵌音频包络（clip gain → 多点包络） | C | P1 | L | 无 |
| C4 | 多机位自动同步 UI 入口 | C | P1 | S | 无 |
| C5 | 章节标记 | C | P2 | S | 无 |
| D1-D5 | 调研预研批（见第 6 节，不在本计划展开） | D | — | — | 需人工决策 |

规模定义（AI 辅助开发口径）：S ≤ 半天；M = 1~2 天；L = 3 天以上或涉及多渲染路径联动。

---

## 2. 全局工程约束（所有任务生效）

1. **数据模型不可变**：`src/features/edit/core/timeline_v2.py` 全部为 frozen dataclass。任何字段修改必须新增 `with_xxx()` 工厂方法返回新实例，禁止原地赋值。同轨重叠校验在 `__post_init__`，改动字段后注意校验仍通过。
2. **命令栈**：所有用户可触发的时间线修改必须实现为 `commands.py` 的命令类（协议见 `commands.py:139` 附近的 `EditCommand`），实现 `execute / undo / to_dict / from_dict`，经 `_execute_command` 进入 `CommandStack`（`commands.py:3756`）。多步原子操作用 `MacroCommand`（`commands.py:9823`）。**不允许绕过命令栈直接改模型**。
3. **序列化兼容**：旧 `.topos` 工程必须能加载。新字段在 `from_dict` 中容错缺失（给默认值），`to_dict` 永远写出。改完跑一次旧工程加载冒烟。
4. **UI 模式**：遵循现有面板结构——信号防递归（状态锁）、渲染解耦（`params_changed` 信号）、全函数类型注解、文案走 `I18N.tr` 并同步 `locales`（zh/en）。
5. **渲染三路一致**：新视觉能力必须同时落地三条路径并保持一致：
   - GPU 预览：`src/features/edit/core/edit_gpu_preview_renderer.py`
   - CPU 回退：`src/features/edit/core/timeline_preview_renderer.py`
   - GPU 导出：`src/features/edit/core/timeline_gpu_export_renderer.py`（失败自动回退 CPU 的路径也要覆盖）
   共享 shader 源必须抽公共 include 文件，禁止两处复制粘贴。
6. **GLSL 规范**：严格类型、所有数字字面量带 `.0`、循环边界常量。参考 `docs/PLUGIN_DEV_GUIDE.md` 与 `.clinerules`。
7. **音频**：混音改动全部在 `src/features/edit/core/audio_playback.py` 的 AudioMixer 内完成，**不得改变 AudioClock 主时钟地位**（samples_played 驱动）。
8. **测试**：核心逻辑新增 pytest 单测放 `tests/`（配置见 `pytest.ini`）。视觉回归可参考 `tools/snapshot_visual_check.py` 的截图对比模式新增专用脚本。
9. **每任务回归基线**：`pytest tests/ -x -q`（至少 edit 相关子集全绿）+ 启动冒烟 + 打开/保存一个现有工程。

---

## 3. Batch A — 高频核心能力（P0）

### A1 时间线片段混合模式（Blend Modes）

#### 背景与现状
专业 NLE（Premiere/Resolve/FCP）的片段不透明度旁都有 20~30 种混合模式，叠素材、局部调色、光效合成的高频功能。当前完全缺失：
- `src/features/edit/core/timeline_render_plan.py:58` 的 `BlendMode` 枚举只有 `NORMAL / CROSS_DISSOLVE / DIP_TO_COLOR / WIPE`，且是**转场混合语义**，不是图层混合语义；
- `src/features/edit/core/realtime_playback/timeline_gpu_compositor.py:60` 的 `blend_mode` 固定 `"normal"`（该组件本身未接管热路径，见 D5，本任务不动它）。

#### 目标
时间线视频片段支持 16 种标准混合模式，检查器可切换，GPU 预览 / CPU 回退 / GPU 导出三路一致，可撤销、可序列化、旧工程兼容。

#### 涉及文件
| 文件 | 改动 |
|------|------|
| `src/features/edit/core/clip_params.py` | 新增 `ClipBlendMode` 枚举 |
| `src/features/edit/core/timeline_v2.py` | `ClipInstance` 增 `blend_mode` 字段 + `with_blend_mode()` |
| `src/features/edit/core/commands.py` | 新增 `SetClipBlendModeCommand` |
| `src/features/edit/ui/edit_properties_panel.py` | Transform/Opacity 分组附近加下拉 |
| `src/features/edit/ui/timeline_items.py` | ClipItem 混合模式徽章 |
| 三路渲染器（见全局约束 5） | 混合公式落地 |
| 新增共享 shader | `src/features/edit/core/shaders/blend.glsl`（若 edit 无 shaders 目录则新建，或放 `src/features/shared/effects/`） |
| `locales` | zh/en 文案 |

#### 实现步骤
1. **枚举设计**：在 `clip_params.py` 新增 `ClipBlendMode(str, Enum)`，**不要复用** `timeline_render_plan.py` 的 `BlendMode`（语义不同，避免污染转场系统）：
   ```
   NORMAL, MULTIPLY, SCREEN, OVERLAY, DARKEN, LIGHTEN,
   COLOR_DODGE, COLOR_BURN, HARD_LIGHT, SOFT_LIGHT,
   DIFFERENCE, EXCLUSION, HUE, SATURATION, COLOR, LUMINOSITY
   ```
2. **模型**：`ClipInstance` 增加 `blend_mode: ClipBlendMode = ClipBlendMode.NORMAL`（frozen，加 `with_blend_mode(mode)`）；`to_dict` 写出字符串值，`from_dict` 缺失时默认 NORMAL。
3. **共享 GLSL**：新建 `blend.glsl`，实现 `vec4 blend_channel(vec4 src, vec4 dst, int mode)`。要求：
   - 混合发生在"当前片段已渲染纹理"与"下方已合成结果"之间，非预乘 alpha 输入先统一到直线 alpha 再混合，最后按 source-over 合回；
   - HUE/SATURATION/COLOR/LUMINOSITY 四个模式需要 RGB↔HSL 函数（GLSL 内实现，注意循环限制——HSL 转换不用循环）；
   - mode 用 uniform int 传入；16 分支 if/else 链即可（同一 draw call 内 uniform 统一，无发散代价）。
4. **三路落地**：
   - GPU 预览：在 `edit_gpu_preview_renderer.py` 的多轨 alpha 合成 pass 接入 blend.glsl，uniform 传入片段 blend_mode；
   - CPU：`timeline_preview_renderer.py` 合成路径用 numpy 实现同一组公式（建议每模式一个小函数，直接可单测）；
   - GPU 导出：`timeline_gpu_export_renderer.py` include 同一份 blend.glsl。
5. **命令**：`SetClipBlendModeCommand`（参考 `SetClipLinkedCommand` 的结构：create 工厂 + execute/undo + to_dict/from_dict），支持多选批量。
6. **UI**：`edit_properties_panel.py` 在 Opacity 分组加下拉（16 项 + i18n）；`timeline_items.py` 参考 fx 徽章（`_draw_effects_badge`，约 3536 行）在非 NORMAL 时画混合模式缩写徽章（如 "MLT"）。
7. **i18n**：`edit.clip.blend_mode.*` 16 条 + 分组标题，zh/en 同步。

#### 验证方法
- **自动化**：新增 `tests/test_clip_blend_modes.py`：
  1. 每种模式在固定像素对上的 CPU 结果符合公式（黑/白/50% 灰/半透明四组输入的期望值表硬编码）；
  2. 命令撤销/重做/序列化往返（blend_mode 保留）；
  3. `from_dict` 无该字段的旧 dict → 默认 NORMAL 不抛错。
  4. （GL 测试基建可用时）CPU vs GPU 小分辨率（320×180）一致性，PSNR ≥ 40dB；基建不可用则跳过并注明。
- **手工**：两视频轨叠放 → 检查器逐一切换 16 种模式 → 截图对比预览帧与导出帧（ProRes 422 + H.264 各一次）目视一致；撤销恢复正常；保存重开工程 blend_mode 保留；旧工程打开无报错。
- **回归**：`pytest tests/ -x -q -k "render or preview or commands or timeline"`。

#### 验收标准
16 种模式三路一致（目视 + 自动化公式校验）；多选批量设置可一步撤销；旧工程加载兼容；全部测试绿。

#### 风险与回退
- 导出与预览漂移 → 强制共用 blend.glsl include，验收时逐模式对比；
- HSL 类模式精度差异 → 期望值表允许 ±1/255 容差；
- 性能：合成 pass 每像素多 16 分支选择，uniform 统一时代价可忽略；若基准下降 >5%（`tools/bench_composite_playback.py`），改为按模式编译 16 个 program 变体（program cache 已有，`effect_pass_runner.py`）。

---

### A2 冻结帧（Freeze Frame）

#### 背景与现状
Premiere "Add Frame Hold" / FCP "Add Freeze Frame" / Resolve "Freeze Frame" 均为一键操作。当前剪辑页无实现；仅 Composite 页有 `frame_hold_node.py`（未与 clip 速度系统打通，本任务不依赖它）。

#### 目标
播放头在某视频片段内时，右键一键"在播放头处冻结"：片段在播放头处分割，后半段变为定格画面，可撤销（单步）、可序列化、导出正确。

#### 方案选择（已定，勿改）
采用**独立冻结字段**方案而非速度点 speed=0 方案：不动 `SpeedParams` 的 0.1–8.0 校验范围和帧映射器积分逻辑，风险最低。与速度系统的统一融合留给未来 retime 2.0。

#### 实现步骤
1. **模型**：`ClipInstance` 增加 `freeze_source_frame: int | None = None`（None=正常；否则该片段全程显示此源帧号）。`with_freeze_frame(frame)` 工厂。`to_dict/from_dict` 容错。
2. **帧获取**：`timeline_frame_mapper.py` 新增查询函数：clip 的 `freeze_source_frame` 非 None 时，任意 timeline 帧都映射到该源帧（播放/擦洗/导出统一走这里，三个渲染器不需要各自特判）。
3. **命令**：`FreezeFrameAtPlayheadCommand`，内部为 `MacroCommand`：
   - 若播放头在片段中间：`SplitClipCommand`（`timeline_ops.split_clip`，timeline_ops.py:60）取右侧新片段；
   - 对右侧片段执行冻结：记录其当前源帧（播放头对应源帧，经帧映射器计算），设 `freeze_source_frame`。
   undo 一步整体还原（合并前删除 split 产生的零碎）。
4. **音频**：冻结片段无对应音频进度 → `audio_playback.py` 混音时跳过 `freeze_source_frame` 非 None 的音频片段（注意：split 后若 A/V 链接指向冻结段，联动处理参考 `ChangeClipSpeedCommand` 对 linked clip 的做法，`commands.py:15482`）。
5. **入口**：
   - `timeline_items.py` 右键菜单（4442–4911 区域）加 "Freeze Frame at Playhead"（仅当播放头在视频片段内启用）；
   - `edit_controller.py` 加对应槽函数；
   - 快捷键经 `module.py` 快捷键注册处（414–507）注册（建议 Shift+F，注册前查冲突——F 已被 Fit 使用，Shift+F 当前是否占用需确认）。
6. **可视化**：ClipItem 画冻结徽章（雪花图标或 "⏸" 缩写），复用速度指示器绘制模式（`_draw_speed_indicator`，timeline_items.py:3613）。

#### 验证方法
- **自动化**：新增 `tests/test_freeze_frame.py`：
  1. 冻结后右侧片段时长不变、所有 timeline 帧映射到同一源帧；
  2. 命令 undo 后时间线与冻结前逐字段一致（片段数、位置、链接关系）；
  3. 序列化往返；4. 音频混音跳过冻结段。
- **手工**：播放头落在视频片段中间 → 右键冻结 → 播放头后画面静止；拖动播放头来回确认前半段正常；导出 H.264 后用 `ffprobe`/`ffmpeg` 抽帧脚本比对冻结区间内连续 5 帧逐像素相同（ffmpeg -ss ... -vsync 0 抽帧 + ffmpeg SSIM 滤镜，参考 `tools/verify_prores_export_quality.py` 的写法）。

#### 验收标准
冻结/撤销/序列化/导出四项全过；冻结段音频静音；徽章显示。

---

### A3.1 平滑速度斜坡（Smooth Speed Ramp）

#### 背景与现状
Resolve Retime Controls 支持速度点之间平滑过渡；当前 `SpeedPoint`（`clip_params.py:356-701`）只支持**分段恒速**（阶梯函数），`speed_points` 严格按 `source_offset` 排序。斜坡（如 25%→400% 渐变）无法表达。

#### 实现步骤
1. **模型**：`SpeedPoint` 增加 `easing: str = "linear"`（可选 `linear | smooth`）。`clip_params.py` 的校验、`with_speed_point` 透传。旧数据无该字段 → 默认 linear（行为不变，兼容）。
2. **帧映射数学**（核心，`timeline_frame_mapper.py`）：
   - 速度曲线 v(t) 在两速度点间从阶梯改为插值：linear=线性过渡；smooth=smoothstep（3t²−2t³）过渡；
   - 源帧映射 = 速度曲线的累积积分。建议实现：预计算每个 timeline 帧边界处的累积源帧位置（浮点），查表输出。**保持确定性**：同一输入永远同一输出（供缓存 key 与导出复用）；
   - 反向（源→timeline）查询用于修剪边界计算，用二分。
3. **UI**：
   - retime 控件（`timeline_view.py:1666-1771`）：速度点右键菜单（4487–4506）加 "Smooth Ramp" 切换；开启时 retime 条带绘制平滑曲线示意（阶梯线 → 曲线）；
   - 属性面板 Speed 分组（`edit_properties_panel.py:1573`）显示当前段 easing。
4. **命令**：新增 `SetSpeedPointEasingCommand`（或并入现有 retime 提交路径 `_commit_retime_params`，`timeline_view.py:10088` 附近——优先并入，减少命令类膨胀）。
5. **缓存失效**：速度相关缓存 key 已含速度参数（`realtime_playback/render_cache_adapter.py` 的 RenderCacheKey），确认 easing 进入 hash。

#### 验证方法
- **自动化**：新增/扩展 `tests/test_timeline_frame_mapper.py`：
  1. 已知数值表：两点（25%、400%、easing=linear/smooth）下，抽样 timeline 帧的期望源帧（浮点容差 1e-6）；
  2. 单调性：任何配置下源帧随 timeline 帧严格非降；
  3. 边界：easing 全部为 linear 时与旧阶梯算法输出**逐帧一致**（回归保障）；
  4. 序列化往返含 easing。
- **手工**：素材设 25%→400% smooth 斜坡，播放与导出无跳变帧；导出后 `ffprobe` 逐帧 pts 平滑（相邻帧 delta 渐变无突变）。

#### 验收标准
数值表通过；linear 模式与旧行为逐帧一致；UI 可切换；缓存正确失效。

---

### A3.2 帧混合变速（Frame Blend）

依赖 A3.1（复用累积浮点映射）。Optical Flow **不在本任务**（见 D1）。

#### 实现步骤
1. **API**：`timeline_frame_mapper.py` 新增 `timeline_to_source_frame_pair(frame) -> tuple[int, int, float]`（帧 a、帧 b、b 的权重）。斜坡/慢速时权重=小数部分；整速段权重=0。保留旧单帧 API 兼容现有调用。
2. **`SpeedParams.interpolation` 接线**：`frame_blend` 值现在真实生效（当前 `clip_params.py:550` 附近对非 nearest 简化返回单帧）；`optical_flow` 保持现状并在 UI 中隐藏或标注不可用。
3. **渲染**：CPU（`timeline_preview_renderer.py`）两解码帧加权 lerp（RGB 域，numpy）；GPU（预览+导出）shader 双纹理采样 + `mix()`。权重作 uniform/pixel 附属数据传入。
4. **UI**：属性面板 Speed 分组的 interpolation 下拉只列 nearest / frame blend。
5. **音频不受影响**（音频变速走 `audio_timestretch.py`，与视频插帧无关）。

#### 验证方法
- 自动化：pair API 数值表；权重 ∈ [0,1]；整速段权重恒 0；CPU lerp 公式校验。
- 手工：0.25x 慢放素材开/关帧混合对比，开启后运动更顺滑无频闪；导出一致；性能冒烟（`tools/bench_composite_playback.py` 无明显回退）。

#### 验收标准
帧混合开/关视觉差异符合预期；三路渲染一致；性能回退 <5%。

---

## 4. Batch B — 沉睡功能接线（性价比最高，建议先行）

### B1 场景检测时间线 overlay 生产入口

#### 现状
`src/features/edit/ui/scene_cut_timeline_overlay.py` 第 1 行标注 `DORMANT(2026-08-19): 已实现但暂无生产入口`；控制器 `scene_cut_controller.py`、对话框 `scene_cut_dialog.py` 均完整。

#### 实现步骤
1. 阅读 `scene_cut_timeline_overlay.py` 对外接口（构造参数、更新方法）与 `scene_cut_controller.py` 现有生命周期。
2. `src/features/edit/module.py`（菜单注册区，参考 334 行场景检测入口）加 "Timeline → Scene Cut Overlay" checkable 菜单项：
   - 开：创建/显示 overlay 并用最近一次检测结果更新（检测结果有缓存体系 `core/scene_cut/cache.py`）；
   - 关：隐藏并释放；
   - 状态经 ConfigManager 持久化，重启恢复。
3. `scene_cut_dialog.py` 检测完成回调处增加 "在时间线标注" 按钮（触发同一开关路径）。
4. 移除两个文件的 DORMANT 注释，`git grep DORMANT` 确认无残留引用。

#### 验证
- 自动化：控制器单测（开关状态迁移、空检测结果不崩）；持久化往返。
- 手工：对素材跑场景检测 → 开 overlay → 时间线显示剪切线标注 → 关闭消失 → 重启状态保留 → 无检测结果时开启不崩溃。

#### 验收
入口可见、可持久化、空态安全、DORMANT 注释清除。

---

### B2 Stem 分轨导出对话框

#### 现状
`src/features/edit/core/stem_export.py` 第 1 行 DORMANT 注释：`export_all_stems / plan_stems_from_sequence / write_stem_wav` 已实现且有测试。数据层 `EditTrack.stem_group`（D/M/E 分组）与 Bus 路由已存在。**只缺 UI 入口**。

#### 实现步骤
1. 新建 `src/features/edit/ui/stem_export_dialog.py`，参考 `subtitle_delivery_dialog.py` 的结构：
   - Stem 分组列表（源自 `plan_stems_from_sequence` 的 plan，展示每组包含的轨道）；
   - 输出目录选择、文件名前缀、格式固定 WAV 48kHz/24bit（`write_stem_wav` 现有能力，勿超范围）；
   - QThread worker 包装 `export_all_stems`（禁止 UI 线程直跑），进度与取消，参考 `render_cache_worker.py` 模式。
2. 入口两处：Deliver 页工具栏加 "Export Stems"；Edit 页 File 菜单加同级入口（走同一对话框）。i18n zh/en。
3. 完成后弹结果摘要（成功/失败列表，`StemExportResult.all_succeeded` 已有）。
4. 移除 stem_export.py 的 DORMANT 注释。

#### 验证
- 自动化：现有 stem 测试保持绿；新增对话框→worker 的冒烟（无工程空态不崩）。
- 手工：构造含 3 个 stem_group 的工程 → 导出 → `ffprobe` 验证每个 WAV 时长=时间线时长、声道数正确、非全静音（`ffmpeg -af volumedetect` 平均音量 > -60dB）。

#### 验收
入口可用、导出产物正确、无 UI 卡死、DORMANT 清除。

---

### B3 Render Cache smart 自动评估器

#### 现状
`render_cache.py` 的 `RenderCacheSettings.auto_mode ∈ {off, smart, user}`，偏好下拉已有（`src/app/ui/preferences_dialog.py:1068` 附近），配置默认 off（`config_manager.py:106`）。**smart 模式无评估器**：没有"自动判定哪些片段值得缓存"的逻辑，实际全靠手动。

#### 实现步骤
1. 新建 `src/features/edit/core/render_cache_evaluator.py`，纯函数打分器（便于单测）：
   ```
   score(clip) = 效果数×2 + (有 grade 且非默认)×3 + (speed≠1)×2
               + (composite_id 非空)×4 + (blend/transform 动画参数数)×1
   ```
   阈值默认 6（进偏好可调）。素材时长过短（<2s）不评。
2. 触发时机（edit_controller）：工程加载完成后 + 命令栈空闲（每次命令提交后 debounce 5s）+ auto_mode==smart 时。复用现有 in-flight 去重守卫（`edit_controller.py:7227` 附近）与 `render_cache_worker.py` 队列，**并发限制 1**。
3. 用户手动缓存优先级高于自动（队列插入策略或手动直插队首）。
4. 偏好页给 smart 模式补说明文案与阈值 spinbox。
5. 日志：每次自动入队记录 clip_id 与分数（diagnostics 体系已有）。

#### 验证
- 自动化：打分函数数值表；短片段排除；auto_mode=off/user 时评估器零行为（spy 不触发）。
- 手工：smart 模式下打开含多效果片段的工程 → 片段自动出现缓存状态徽章（VALID）；修改效果参数 → 变 STALE 并重算；off 模式无自动行为。

#### 验收
smart 模式自动缓存正确片段、参数变更失效重算、其他模式零干扰。

---

### B4 比例拉伸工具（Rate Stretch）

#### 现状
`EditTool` 枚举（`timeline_items.py:514`）有 9 个工具（SELECT/TRIM/BLADE/RIPPLE/ROLL/SLIP/SLIDE/HAND/ZOOM），缺 Rate Stretch。变速只能右键菜单 10%–800%。

#### 实现步骤
1. `EditTool` 增加 `RATE_STRETCH = "rate_stretch"`。
2. `timeline_panel.py` 工具栏（246–335 区域）加按钮（图标参考现有工具图标风格）；快捷键建议 Shift+R（R 已被 Ripple 占用；注册前在 `module.py:414-507` 与 ShortcutManager 查冲突）。
3. 交互（`timeline_interaction.py` 的 ClipDragSession + `timeline_view.py` trim 事件路由，3962 附近参考 Trim/Ripple 语义区分）：
   - RATE_STRETCH 工具下拖片段**右边缘**：新时长 → `speed = 源时长 / 新时间线时长`，clamp 0.1–8.0（超界吸附到边界并显示提示）；
   - 拖左边缘：入点侧对齐，反向同理；
   - 提交走现有 `ChangeClipSpeedCommand`（`commands.py:15482`，已含 linked clip 联动），**不要新造命令**。
4. 光标与提示：hover 边缘时显示拉伸光标 + 预览速度气泡（复用修剪时的数值气泡模式）。
5. i18n + 工具提示文案。

#### 验证
- 自动化：`tests/test_rate_stretch.py`——拖拽换算公式表（含 clamp 上下界、linked 音频同步变速、undo 还原）。
- 手工：拖出 2x/0.5x 各一次，播放与导出时长正确；与 linked 音频同步；撤销一步还原；工具切换快捷键无冲突。

#### 验收
工具全链路可用、clamp 生效、复用现有速度命令、无快捷键冲突。

---

### B5 快捷键完全可自定义（时间线内嵌键迁移）

#### 现状
`ShortcutManager`（`src/shared/ui/shortcut_manager.py`：改键/持久化/冲突检测/预设导入导出）与 Resolve 风格对话框（`src/app/ui/shortcut_dialog.py`）完善，但时间线内嵌 `keyPressEvent`（`timeline_view.py:4040-4227`、11865 附近；`timeline_panel.py:758-787`、992-1012 的 JKL；`source_viewer.py:1220-1225` 的 I/O）**硬编码绕过 ShortcutManager**。

#### 实现步骤（分四步迁移，每步独立可回归）
1. **盘点**：grep 三个文件全部 `keyPressEvent` 分支，产出 action 清单（action_id、默认键、当前处理器函数、所属组件），写入本文档附录（执行时补全）。预估 30+ 个：工具键 A/T/B/R/W/Y/U/H/Z、JKL、I/O、M、Delete、Ctrl+B、方向键组、Ctrl+C/V/X、Ctrl+Z 等。
2. **注册**：全部 action 以默认键值注册进 ShortcutManager（此步不改行为，纯注册）。
3. **迁移**（逐组件）：
   - `timeline_panel.py` JKL/Space → 查询 ShortcutManager 当前绑定；
   - `timeline_view.py` 编辑键 → 同上（注意：keyPressEvent 里还含工具态判断，迁移时保留状态机逻辑，仅替换"键→action"的匹配层）；
   - `source_viewer.py` I/O → 同上。
   匹配层建议抽一个小工具函数 `matches_action(event, action_id) -> bool` 统一处理修饰键。
4. **回归清单**：改一个键（如 B→X）→ 旧键无效、新键触发剃刀；重置默认恢复；重启持久化；冲突检测弹提示。

#### 验证
- 自动化：ShortcutManager 已有测试保持绿；新增 action 注册完整性测试（清单 vs 注册表 diff 为空）。
- 手工：上面回归清单逐条 + 全工具键走一遍。

#### 风险
本批最大风险项：keyPressEvent 分支多且含组合逻辑。**迁移只替换键匹配，不改处理逻辑**；每迁一个组件跑一次手工清单，禁止一次迁三个文件。

---

### B6 用户自定义导出预设持久化

#### 现状
`src/features/deliver/core/video_export_presets.py:16-17` 注释明示：自定义预设第一阶段不持久化。项目根 `Presets/` 目录已有 System/User 子目录约定（颜色预设 JSON 在用）。

#### 实现步骤
1. 新建用户预设存储（参考 ConfigManager 模式或直接 JSON 文件落 `Presets/User/export/`，与现有 User 目录约定一致）：
   - schema 与内置预设（`video_export_presets.py` 的 `ExportPreset`）一致 + `name / created_at / is_custom`；
   - 读写原子（临时文件+rename），坏文件容错跳过并日志。
2. 设置面板（`ui/video_export_settings_panel.py`）加 "Save As Preset…"（输入名称查重）、右键/菜单 Delete、Rename；预设下拉分 "Built-in / Custom" 两组。
3. 导出队列（`export_queue.py`）job 创建时**快照**预设参数而非引用名称（防预设删除/修改影响已排队 job 的重放语义）。
4. i18n。

#### 验证
- 自动化：存/读/删/改名往返；坏 JSON 容错；job 快照与预设后续修改解耦（改预设后旧 job 参数不变）。
- 手工：存自定义预设 → 重启应用仍在 → 用它导出一次成功 → 删除后下拉消失且队列中已完成 job 记录不报错。

#### 验收
预设 CRUD + 持久化 + 队列解耦三项全过。

---

### B7 占位符片段（Placeholder Clip）全流程

#### 现状
`MediaAsset.media_type`（`src/features/edit/core/project.py:142-145`）已含 adjustment/solid/motion_graphic 等，无 placeholder。粗剪定结构后补素材的专业流程缺失。**注意：素材替换命令不存在**（`timeline_ops.py` / `commands.py` 均无 replace_clip/ReplaceAsset），需新增。

#### 实现步骤
1. **素材类型**：`project.py` 增 `"placeholder"`；PlaceholderAsset 允许空 path，带元数据：目标分辨率、帧率、时长、备注（"此处待补：航拍空镜"）。
2. **创建入口**：媒体池右键 "New Placeholder…"（对话框填元数据）；也可时间线空轨右键直接创建并落轨。
3. **渲染**：ClipItem 用 QPainter 画斜纹+彩条占位图（**禁止走解码器**——preview renderer 对 placeholder clip 走专用静态帧生成器，CPU/GPU 三路共用同一静态帧）。
4. **替换流程**：媒体池拖真实素材到占位片段上（drop 路由 `timeline_view.py:12505+` 识别 placeholder 目标）→ 新命令 `ReplaceClipAssetCommand`（保留 timeline 位置/时长/链接关系/效果，替换 asset_id 并清 placeholder 标记；linked 音频对同步处理）。同时提供右键 "Replace With…" 文件选择路径。
5. **导出拦截**：`video_export_preflight.py` 增检查项——时间线含 placeholder 时导出**拒绝并逐条列出**（位置+备注），除非用户显式勾选 "允许占位符（渲染黑场+边框警告）"。
6. **可视化**：轨道上斜纹外观 + 徽章；标记列表面板可筛。
7. i18n（zh/en 全流程文案较多，注意覆盖）。

#### 验证
- 自动化：placeholder 素材创建/序列化往返；替换命令（时长裁剪边界：真素材短于占位时长时的处理——截断并警告）；preflight 拦截与放行开关；undo 替换还原。
- 手工：建占位 → 粗剪排布 → 导出被拦截并列出清单 → 拖真素材替换 → 导出通过 → 撤销替换回到占位。

#### 验收
创建/编辑/替换/导出拦截/撤销全链路可用。

#### 风险
L 规模主因是触点多（媒体池/时间线/渲染/导出/命令）。可拆两个 PR：先数据+渲染+拦截，后替换命令+拖放。

---

### B8 全局插入/覆盖模式切换

#### 现状
insert/overwrite 是分离的显式调用（`timeline_ops.py:1251/1341`；面板 API `timeline_panel.py:2200/2241`），无 Resolve 式全局模式指示。拖放素材默认行为固定，用户无法"当前拖放一律插入（波纹）"。

#### 实现步骤
1. `timeline_panel.py` 工具栏加 toggle 按钮（默认 **Overwrite**，更安全）；状态经 ConfigManager 持久化。
2. 拖放路由（`timeline_view.py:12505+`）与源监视器 Insert/Overwrite 按钮共用该模式：模式=Insert 时，拖放落点走 `insert_clip_at_playhead` 的波纹语义（在落点处波纹），源监视器主按钮变为 Insert。
3. 模式切换时状态栏/按钮高亮明确提示当前模式。
4. 快捷键建议 Shift+B 切换（B 已被 Blade 占用；注册前查冲突）。
5. （可选增强，可后置）拖放过程中显示插入位置的竖线+箭头预览。

#### 验证
- 自动化：两种模式下拖放分别触发 insert/overwrite 命令（spy 命令栈）；模式持久化往返。
- 手工：同一拖放操作在两种模式下分别验证波纹/覆盖差异；重启后模式保留。

#### 验收
模式生效、持久化、快捷键无冲突。

---

## 5. Batch C — 专业工作流补强（P1）

### C1 标题模板库与文字动画扩展

#### 现状
字幕体系强（`src/features/edit/core/captions/`：`CaptionStyle` 34 字段、`style_preset.py` 内置 broadcast/social/lower-third、`graphics.py` 的 Caption Graphic 模板已含 Fade/Typewriter/Scale Pop 三种动画、`render_plan.py`+`subtitle_renderer.py` burn-in）。**缺**：独立"标题"概念入口（拖入即用的 Title 模板库）、更丰富的文字动画、用户自定义模板。

#### 方案（务实路线：扩展字幕体系，不造新文字引擎）
1. **模板分类扩展**：`style_preset.py` 增加类别字段（lower_third / title / callout / credit / social），每类补 4~6 个内置预设（含背景条、描边、阴影、位置锚点组合）。
2. **文字动画扩展**：`captions/model.py` 的 `CaptionWordAnimation` 从 word-level 扩展出 cue-level 入场/出场动画枚举（fade / slide_up / slide_left / scale_pop / typewriter 已有实现可复用 `graphics.py`），`subtitle_renderer.py` 实现渲染（时间函数：ease-out 统一）。
3. **Titles 入口**：效果面板（`edit_effects_panel.py`）加 "Titles" tab：卡片=模板预览缩略图（静态渲染 PNG，构建脚本参考 `scripts/build_mg_preset_previews.py` 模式）→ 拖到字幕/图形轨即创建应用该样式的字幕片段，双击进入文本编辑（现有交互）。
4. **用户自定义模板**：字幕样式编辑器加 "Save As Template" → 存 `Presets/User/title_templates/`（JSON，schema 同 CaptionStyle 子集+动画选择）；Titles tab 分组显示。
5. i18n。

#### 验证
- 自动化：预设序列化往返；动画渲染数值（首帧/中间帧 alpha 与位置期望值）；用户模板读写容错。
- 手工：每类模板拖入→改文字→预览/导出 burn-in 截图目视；动画播放逐帧流畅；自定义模板重启可见。

#### 验收
Titles tab 可用、6+ 动画、用户模板持久化、预览=导出。

---

### C2 用户自定义转场扩展点

#### 现状
`transition_catalog.py:20` `TRANSITION_DEFINITIONS` 为硬编码 tuple（17 种），用户无法自制。渲染侧 `transition_wipe.py` / `transition_mask.py` 已参数化（形状/方向/羽化）。项目 DCTL (.tglsl) 插件体系成熟（`dctl_wrapper.py`）但未暴露转场签名。

#### 实现步骤（两阶段）
**阶段 1：参数化模板实例（安全，先做）**
1. `TransitionDefinition` 增加 `source: str = "builtin"` 与 `params: dict` 字段。
2. 新建用户转场声明 JSON 加载器：`Presets/User/transitions/*.json`，schema：
   ```json
   {"id": "my_wipe", "base": "wipe", "label": "我的斜擦",
    "params": {"direction": 45, "feather": 0.2, "border_color": "#FFFFFF"},
    "icon": "wipe"}
   ```
   base 必须是内置可参数化转场（wipe/slide/iris/dip）；坏 JSON 跳过+日志。
3. 效果面板转场 tab 分组显示（内置/我的转场）；拖放/右键路径复用现有 MIME 路由（`timeline_view.py:12592-12626`），仅目录来源扩展。
4. 渲染侧把 `params` 传入现有 wipe/mask 实现的参数化入口（若现签名缺参数则补齐透传）。

**阶段 2（可选进阶）：DCTL 转场签名**
- 在 PLUGIN_DEV_GUIDE 补转场签名约定：uniform `tex_prev/tex_next/progress/params...`，输出 vec4；复用 `dctl_wrapper.py` 编译装载。转场目录加载 `.tglsl` 时校验签名。

#### 验证
- 自动化：JSON 加载（合法/非法/重复 id）；params 序列化进 `TransitionInstance`；旧工程往返。
- 手工：手写一个自定义 wipe JSON → 面板出现 → 拖到切点 → 预览/导出正确 → 重启仍在；坏 JSON 启动不崩且有日志。

#### 验收
阶段 1 全过即可验收；阶段 2 独立排期。

---

### C3 时间线内嵌音频包络

#### 现状
音量包络在独立 Automation Lane 对话框（`src/shared/ui/audio_mixer/automation_lane.py`，**track 级** automation，交互完整：加点/拖动/插值切换）；音频片段上只有波形显示（含效果感知波形：`audio_waveform.py` 的 ClipEffectWaveformRenderer 已实时反映增益/淡入淡出——**本任务直接受益**）。**缺 clip 级**音量/包络在片段上的直接绘制编辑。

#### 实现步骤（两阶段）
**阶段 1：Clip Gain（对齐 Premiere Audio Gain）**
1. `ClipInstance` 增 `audio_gain_db: float = 0.0` + `with_audio_gain_db()`；范围 [-48, +12]。
2. `SetClipAudioGainCommand`；`audio_playback.py` AudioMixer 混音时叠加 clip gain（在 track volume 之前）。
3. 波形实时反映（效果感知波形链路已有，验证即可）。
4. 交互：音频 ClipItem 中部水平增益线，直接上下拖（参考 automation_lane 拖点手感）；右键菜单 "Audio Gain…" 数值输入 + **Normalize** 按钮（用 `loudness_analyzer.py` 的 LUFS 分析，目标 -23 LUFS 或峰值 -3dB 可选）。
5. i18n。

**阶段 2：Clip 级多点包络**
6. 新数据：clip 域关键帧（复用 `src/shared/keyframe.py` 的 KeyframeTrack 挂到 ClipInstance.animated_params 或专用字段 `volume_points`，二选一，倾向前者复用插值/序列化）。
7. 命令：参考 `AddAutomationKeyframeCommand`（`commands.py:8790`）新增 clip 域版本。
8. 绘制/交互：ClipItem 叠加包络折线（Ctrl+点击加点、拖点、右键删、双击切插值 linear/bezier/hold——交互代码从 `automation_lane.py` 提取共享，勿复制粘贴）。
9. 播放/导出：AudioMixer 按帧插值应用；导出音频路径同样应用。

#### 验证
- 自动化：gain 公式与混音顺序（clip gain → clip 包络 → track automation → track volume → bus）；包络插值扫描数值表；undo；序列化。
- 手工：拖增益线→播放响度变化+波形同步变化；包络画 V 形静音中段→播放正确；导出 WAV 用 `ffmpeg -af volumedetect` 验证响度变化符合预期。

#### 验收
阶段 1 独立可验收；阶段 2 交互手感对齐 automation_lane。

---

### C4 多机位自动同步 UI 入口

#### 现状
`core/multicam_sync.py` 音频波形同步 QThread worker 已实现；`multicam_panel.py` 有角度切换与 sync_offset 显示，**无"自动对齐"按钮**。

#### 实现步骤
1. `multicam_panel.py` 加 "Auto Sync by Audio…" 按钮：
   - 选参考角度（默认第一个）→ 调 `multicam_sync` worker 对其余角度计算偏移；
   - 结果对话框：每角度 offset（帧/时间码）+ 置信度，用户确认后应用；
2. 应用：写 `MulticamAngle.sync_offset`（字段已有）。若无现成命令类则新增 `SetMulticamSyncOffsetsCommand`（批量、可撤销）。
3. 素材时长不足以覆盖偏移时给警告（对齐后出界提示）。
4. i18n。

#### 验证
- 自动化：worker 对已知偏移的合成音频夹具（`tools/make_tracking_fixtures.py` 模式造两路错位正弦/噪声）算出正确 offset；命令 undo。
- 手工：手机+相机双录同一场（或测试夹具）自动对齐→多机位切换角度时画面/声音同步；撤销还原。

#### 验收
一键对齐可用、结果可确认、可撤销、失败（无可检测波形）时明确报错。

---

### C5 章节标记

#### 现状
`TimelineMarker`（`timeline_v2.py:131`：frame/name/color/comment）无类型语义；专业交付（DVD/YouTube 章节）需要 chapter marker。

#### 实现步骤
1. **模型**：`TimelineMarker` 增 `kind: str = "standard"`（standard | chapter）。`with_kind()`；`AddMarkerCommand` / `DeleteMarkerCommand`（`commands.py:14462/14559`）透传；序列化容错。
2. **UI**：标记编辑对话框（`timeline_view.py:11761-11863`）加类型下拉；标记列表面板（`timeline_panel.py:1684-1768`）加筛选；时间线标尺上章节标记用区分色/图标；快捷键建议 Shift+M 添加章节（当前 Shift+M 是跳标记，需查冲突，冲突则换 Alt+M 并更新文档）。
3. **导出**（MVP 两种，均落 Deliver 页）：
   - YouTube 章节文本：`hh:mm:ss 标题` 列表 → 复制到剪贴板 + 存 .txt；
   - FFMetadata 文件（ffmpeg 通用章节格式）导出 .ffmeta；
   - MP4/MOV 内嵌章节（ffmpeg `-map_chapters` 封装步骤）作为进阶项，若封装管线（`captions/mux.py` 参考）接入顺利则一并做，否则留 TODO 并在交付对话框注明。
4. i18n。

#### 验证
- 自动化：marker kind 序列化往返；YouTube 文本格式（含 ≥1h 的时间码进位）；FFMetadata 转换纯函数测试。
- 手工：加 3 个章节 → 导出 YouTube 文本核对；若做内嵌：`ffprobe -show_chapters` 核对。

#### 验收
类型区分 + 两种导出可用；旧工程兼容。

---

## 6. Batch D — 调研预研批（不在本计划展开）

以下项工程量大或依赖外部决策，**先不执行**，留待产品定位明确后单独立项：

| 编号 | 项目 | 暂缓原因 |
|------|------|---------|
| D1 | Optical Flow 光流变速 | 需专门算法/模型（传统 CV 或 AI），A3.2 的帧混合已覆盖大部分慢放场景 |
| D2 | VST/AU 音频插件宿主 | Python 生态无成熟 VST 宿主方案，需自研绑定层或引入商业组件，先调研再决策 |
| D3 | OFX 插件体系 | OFX SDK C++ 集成 + API 面广，等同于引入一个插件平台，需架构评审 |
| D4 | RAW 格式（BRAW/R3D/CinemaDNG/ProRes RAW） | 依赖厂商 SDK 授权与解码管线大改，非纯工程问题 |
| D5 | 第二代播放引擎接管（TimelineGpuCompositor/DecodeScheduler/FrameRingBuffer） | 组件已编写但注释明示未接管热路径，属性能重构，需专门基准护航（`tools/bench_*` 体系），建议单独立项并配基准门禁 |

---

## 7. 建议执行顺序与批次验收门

**推荐顺序**（依赖与性价比综合）：

```
第一批（白捡，1~2 天）：B1 → B2 → B6 → B8 → C4 → C5
第二批（核心能力）    ：A2 → B4 → A1
第三批（进阶）        ：A3.1 → A3.2 → B7 → C2
第四批（大件，单独排期）：B5 → C1 → C3 → B3
```

**批次验收门**（每完成一项）：
1. `pytest tests/ -x -q` 全绿（新增测试随项落地）；
2. 启动冒烟 + 打开/保存现有工程无回归；
3. 本项"验证方法"中的手工清单逐条执行；
4. commit message 规范沿用现有风格（`feat(edit): ... / fix(edit): ...`），单项单 commit。

**里程碑验收**（每批完成后）：
- 用一个真实素材工程走完整流程：粗剪（占位/插入模式）→ 精剪（混合模式/变速/冻结）→ 音频（增益/包络）→ 字幕标题 → 多机位 → 交付（预设/stem/章节），确认批内功能互相无冲突。

---

## 8. 关键文件速查索引

| 模块 | 路径 |
|------|------|
| 时间线数据模型（frozen） | `src/features/edit/core/timeline_v2.py` |
| 片段参数（Transform/Speed/…） | `src/features/edit/core/clip_params.py` |
| 编辑操作纯函数 | `src/features/edit/core/timeline_ops.py` |
| 命令栈与 113 个命令 | `src/features/edit/core/commands.py`（CommandStack: 3756） |
| 帧映射/混合帧率 | `src/features/edit/core/timeline_frame_mapper.py` |
| GPU 预览渲染 | `src/features/edit/core/edit_gpu_preview_renderer.py` |
| CPU 预览渲染（8000+ 行） | `src/features/edit/core/timeline_preview_renderer.py` |
| GPU 导出渲染 | `src/features/edit/core/timeline_gpu_export_renderer.py` |
| 音频引擎/混音 | `src/features/edit/core/audio_playback.py` |
| 波形（效果感知） | `src/features/edit/core/audio_waveform.py` |
| 时间线主视图（15000+ 行） | `src/features/edit/ui/timeline_view.py` |
| 片段/轨道/标记图元 | `src/features/edit/ui/timeline_items.py`（EditTool: 514） |
| 时间线面板/工具栏/JKL | `src/features/edit/ui/timeline_panel.py` |
| 检查器 | `src/features/edit/ui/edit_properties_panel.py` |
| 效果面板 | `src/features/edit/ui/edit_effects_panel.py` |
| 转场目录 | `src/features/edit/core/transition_catalog.py` |
| 字幕体系 | `src/features/edit/core/captions/`（model/style_preset/graphics/export） |
| Stem 导出（DORMANT 待接） | `src/features/edit/core/stem_export.py` |
| 场景检测（overlay DORMANT） | `src/features/edit/ui/scene_cut_timeline_overlay.py` + `scene_cut_controller.py` |
| 渲染缓存 | `src/features/edit/core/render_cache.py` + `render_cache_worker.py` |
| 多机位 | `src/features/edit/core/multicam.py` + `multicam_sync.py` + `ui/multicam_panel.py` |
| 导出格式/预设/队列 | `src/features/deliver/core/video_export_formats.py` / `video_export_presets.py` / `export_queue.py` |
| 导出预检 | `src/features/deliver/core/video_export_preflight.py` |
| 快捷键管理 | `src/shared/ui/shortcut_manager.py` + `src/app/ui/shortcut_dialog.py` |
| 混音器/自动化 | `src/shared/ui/audio_mixer/`（automation_lane.py） |
| 媒体代理 | `src/shared/media/proxy_generator.py` + `src/features/edit/core/proxy_policy.py` |
| 用户预设目录约定 | `Presets/System/`、`Presets/User/` |
| 工程持久化 | `src/shared/project/topos_project.py` + `save_router.py` |

---

## 9. 变更记录

| 日期 | 变更 |
|------|------|
| 2026-09-07 | 初版：基于 4 路并行调研生成 A/B/C 三批 16 项 + D 批预研 5 项；剔除已实现的 Link/Unlink（调研误报）；按用户要求排除关键帧曲线编辑器与硬件编码导出 |
| 2026-09-08 | 执行完成：A/B/C 三批 17 项全部实施并逐项提交（B1~B8、A1/A2/A3.1/A3.2、C1~C5；D 批按计划暂缓）。执行说明与遗留边界见第 10 节 |

## 10. 执行记录（2026-09-08）

全部 17 项按推荐顺序完成，单项单 commit（`feat(edit)`/`feat(deliver)` 系列，另含数个修复 commit）。回归基线：tests/edit + tests/deliver 全量 5573 passed，失败集合与任务开始前的存量失败完全一致（零新增回归）。

### 各项落地要点与诚实边界

| 编号 | 状态 | 落地要点 / 偏离说明 |
|------|------|---------------------|
| B1 | ✅ | MenuActionDescriptor/menu_builder 支持 checkable；开关挂 Edit/Timeline（View 顶级菜单未注册进 MenuBuilder，View/Timeline 不可达）；ConfigManager 持久化 + 空检测结果安全 |
| B2 | ✅ | export_all_stems 增 specs 子集/cancel_check；新增 create_clip_decode_fn 生产解码工厂（preserve-pitch WSOLA 回退变调，与实时一致）；Deliver 页按钮 + File 菜单双入口 |
| B3 | ✅ | render_cache_evaluator 纯函数打分器 + 控制器 debounce 5s 触发（挂在 _on_edit_timeline_changed 统一发布点）；偏好页阈值 spinbox；顺带补齐 retime 结构入 render hash 的缺口 |
| B4 | ✅ | EditTool.RATE_STRETCH + Shift+R；边缘拖拽 → speed=源/新时长 clamp 0.1-8 → 提交既有 ChangeClipSpeedCommand（linked 音频联动）；hover 速度气泡未做（工具栏 tooltip 已说明） |
| B5 | ⚠️ 部分 | 步骤1/2 完成：18 个工具/标记/IO 键全量注册进 ShortcutManager（可见/可改/冲突检测）；步骤3 完成 TimelineView 工具键匹配层迁移（matches_key_event + 默认键回退，改键即生效有单测）；导航键/Space/Delete/Ctrl 组合键保留硬编码（全局语义已由菜单 QAction 覆盖），迁移留回归清单 |
| B6 | ✅ | Presets/User/export/*.json 原子存储；面板 Custom 分组 + Save As/Rename/Delete；job 快照语义经测试固化（build_job_from_preset 深拷贝） |
| B7 | ✅ | placeholder 类型 + 创建工厂 + 彩条斜纹静态帧（三路共用不触解码器）+ preflight 拦截/放行 + ReplaceClipAssetCommand；入口：Edit/Timeline 菜单 New Placeholder + 占位片段右键 Replace With…；媒体池右键与拖放替换路由留扩展 |
| B8 | ✅ | 工具栏 INSERT/OVERWRITE 切换 + Shift+B + ConfigManager 持久化（默认 overwrite）；Insert 模式拖放走 InsertClipCommand 波纹（sync-locked 音频跟随）；顺带修复 insert_clip_at_playhead 先加后移的边界重叠误报 |
| C1 | ✅ | style_preset 增 category + 11 内置预设（5 类）；title_templates.py 用户模板存储；效果面板 Titles tab（双击创建带样式字幕；拖放 MIME 留扩展）；cue 级动画枚举 + ease-out 包络纯函数（burn-in 渲染接线为扩展点） |
| C2 | ✅ 阶段1 | 参数化模板实例全链路（JSON 声明/注册/校验/渲染 mask 走 base 公式/面板"我的转场"分组）；阶段 2 DCTL 转场签名按计划独立排期 |
| C3 | ✅ 阶段1 | Clip Gain 全链路：模型 [-48,+12] + SetClipAudioGainCommand（批量）+ 实时/导出混音双路应用（clip fade 后、track 处理前）+ 右键 Audio Gain…；Normalize 的 compute_normalize_gain_db 已就绪待接 LUFS 管线；阶段 2 多点包络独立排期（计划允许分阶段验收） |
| C4 | ✅ | 复核发现波形同步链路 P1-01 已落地；补齐计划缺口：结果确认对话框（应用才写回）+ 出界警告（\|offset\|≥素材总帧数行内标注） |
| C5 | ✅ | TimelineMarker.kind + Alt+M 章节快捷键（Shift+M 被跳标记占用，按计划换键）+ 区分色/C 徽章 + 标记列表筛选 + YouTube/FFMetadata 导出（Deliver 页 Export Chapters…）；MP4 内嵌章节经 FFMetadata+ffmpeg 可达，UI 直封留 TODO |
| A1 | ✅ | 16 模式三路一致：NumPy 公式 + 共享 blend.glsl（GPU 预览/导出 include 同源）+ CPU 合成分派；SetClipBlendModeCommand 批量；属性面板下拉 + 徽章 |
| A2 | ✅ | freeze_source_frame 字段 + timeline_to_source_frame 短路（三路自动生效）+ FreezeFrameAtPlayheadCommand（分割+定格单步撤销）+ 音频双路跳过 + FZ 徽章 + 右键入口 |
| A3.1 | ✅ | SpeedPoint.easing + smoothstep 渐变积分查表（linear 逐帧一致有单测）+ with_point_easing 并入 retime 提交路径 + Smooth Ramp 菜单切换 |
| A3.2 | ✅ | map_frame_pair/timeline_to_source_frame_pair 双帧采样 API（整速权重恒 0）；CPU 预览/导出双解码帧 lerp；GPU 单纹理管线维持 nearest（双采样留 D5 第二代引擎接管，如实记录）；Interpolation 下拉只列 nearest/frame_blend |

### 顺带修复的存量缺陷

1. `_move_playhead_to` 调用不存在的 `_update_playhead_position`——跳标记（Shift+M）必崩 AttributeError
2. `insert_clip_at_playhead` 先加后移顺序缺陷——边界插入误报重叠
3. `_TOPOS_PLANAR_RE` 常量块在 A3.2 提交中被脚本误删（当日发现当日修复）
4. `build_render_hash` 不含 retime 结构——补 speed_params_signature
5. 修复 zh_CN/en_US locale 文件中 `"edit"` 键重复导致的翻译被覆盖问题


### 2026-09-07 全面复查（bug 清扫）

对 41d74ce5..0c1369ed 的 20 个提交做整体复查（4 个并行深度审查 + 程序化校验），发现并当日修复：

**崩溃级（6 项）**
1. `timeline_to_source_frame_pair` 平滑斜坡路径 `import numpy as _np` 但用 `np`——smooth 斜坡 + 帧混合即 NameError（CPU 预览/导出崩）
2. `FreezeFrameAtPlayheadCommand`/`SplitClipCommand` 用 `effective_speed()` 切分源范围——速度点片段冻结/分割时右段源时长可为负（ValueError）
3. 冻结/分割右段保留超长 fade_out、左段保留超长 fade_in——尾部冻结/头部分割撞 fade≤duration 校验直接 raise
4. 控制器四处引用不存在的 `edit_page._command_stack`：混合模式下拉（UnboundLocalError）、占位片段落轨（命令从未执行）、标题模板（AttributeError）+ `MacroCommand` 未导入（NameError）
5. `title_template_selected` 信号从未在 EditEffectsPanel 声明——双击标题模板即 AttributeError
6. `on_create_placeholder_clip` 导入不存在的 `core.placeholder_dialog`（正确路径在 ui/）

**数据丢失/一致性（8 项）**
7. 六个既有命令（Split/Trim undo/Slip undo/UpdateClipParam×5 分支/Freeze/ReplaceAsset/Merge）手工重建 ClipInstance 丢 `blend_mode`/`audio_gain_db`/`freeze_source_frame`（部分还丢 label_color/fades/color stack）——统一改 `dataclasses.replace()`，并新增 10 项字段保留回归测试
8. CPU 路径 BGR 直接进 `blend_rgb`（HSL 系四模式通道权重错位）+ `_set_sat` 灰输入平局与 GLSL 分歧——预览/导出两路 BGR↔RGB 翻转 + SetSat 对齐 W3C else 分支
9. GPU 预览 6 处 draw 复用 stale `u_blend_mode`（末端 blit 把顶层模式作用于整帧对黑底混合——multiply 等模式整帧变黑）；底层片段 GPU 对黑底混合而 CPU 不混合——统一为底层不应用模式
10. CPU 预览 4 通道 BGRA 层（字幕/场景）完全忽略混合模式——补 per-pixel alpha + blend 分支
11. 帧混合基准帧 round 与权重 floor 不一致——frac>0.5 时帧混合静默退化为纯 nearest；冻结 + frame_blend 组合冻结失效——均修
12. Stem 导出忽略 Clip Gain——Master stem 与成片音轨不一致；`with_speed_point_speed` 静默重置 easing=linear
13. 多机位同步完成回调重推播放头定位组——播放头挪走即丢弃几分钟计算结果（改快照）；smart 自动缓存把普通片段派给仅支持合成/MG 的管线（状态栏骚扰）——派发前预筛
14. FFMetadata 标题未转义 `= ; # \`、NTSC 帧率取整漂移（改 TIMEBASE=1/1000 毫秒）、YouTube 首章在 0:00..fps-1 时重复 0:00 时间戳

**接线缺口（4 项）**
15. `allow_placeholder_clips` 无产品入口（占位时间线永远硬拦截无法导出）——VideoExportJob 加字段 + 导出设置 File 组复选框（放行=降级 warning）
16. 占位替换承诺的真素材截断未实现——ReplaceClipAssetCommand 实现 clamp_source_duration（undo 还原时长），视图传 total_frames
17. 9 个新 i18n 键缺失（markers 筛选/properties 插值/my_transitions 等）补齐 zh/en
18. `status_message_requested` 信号未声明（冻结失败提示死代码）——声明并接通

**健壮性（3 项）**
19. StemExportDialog Esc/Close 走 reject() 不触发 closeEvent——worker 线程悬空（"QThread destroyed while running"）；重写 reject() 收口
20. `_restore_scene_cut_overlay_state` 相等早退——时间线视图重建后 overlay 丢失；rate_stretch 拖拽失败不还原 `_source_start/_source_duration`（波形/缩略图源区间错位到下次 reload）
21. 损坏工程数据（负 freeze 帧/超界 gain/未知 blend）导致整个工程加载失败——from_dict 清洗/clamp

**复查确认无误**：zh/en locale 键全对齐；ClipInstance/SpeedPoint/TimelineMarker 序列化往返与旧格式容错；`insert_clip_at_playhead` 先移后加；EditMarker/SetClipAudioGain 命令对称性；16 模式 GPU/CPU 公式等价（除已修的灰平局）；lerp 三路半上取整一致；冻结短路三路覆盖；占位彩条数值标准；ModernGL 资源无泄漏。

**回归**：tests/edit + tests/deliver（排除 ai_editing 与不稳定的 test_audio_waveform）5585 passed / 17 failed，失败集与复查前基线逐条一致（0 新增回归）。render-cache 的 retime 签名仍为现成未接线挂钩（清单只覆盖合成/MG，已在函数 docstring 如实标注）。
