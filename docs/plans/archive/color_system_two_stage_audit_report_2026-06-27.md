# 色彩系统两阶段与 Output 渲染输出审查报告

日期: 2026-06-27
范围: `df07fe52..HEAD`
当前分支状态: `dev...origin/dev`
审查对象:

- `b63964b2 feat(composite): Output 节点渲染输出 + 合成片段设置 + 色彩修复`
- `136a3e45 fix(color): merge 对齐识别 bt709 + viewport display_transfer 状态同步`
- `f0a0f001 feat(color): 第二阶段 — 宽色域视频 + HDR(含预览色调映射) + bundle ICC`
- `4b9c3dc8 fix(color): 修复色彩系统审查发现的 11 个问题`
- `6b0c95d9 Merge branch 'fix-ui' into dev`

变更统计: 44 个文件，`+6461/-572`。这和会话描述中的 `+6187/-572` 不完全一致，原因是当前 HEAD 已经是合并后的 `dev`，并且本地显示 `fix-ui` 已存在远端跟踪记录，不再是“未推送”的状态。

## 总结结论

不能判定为“完美完成”。结构化 `ColorSpaceDescriptor`、Rec.709/sRGB 分离、直接 `VideoFrameNode` 的 BT.709/PQ/HLG 识别、IDT shader 入口、viewport `display_transfer` 状态恢复、merge 对齐识别 `bt709` 等核心方向是正确的；聚焦单测也通过。

但是审查发现仍有多个会让功能静默失效或明显错误输出的隐藏 bug。最需要优先修的是 5 个阻断级问题:

1. bundled OCIO config 当前无法加载，所谓“OCIO 必需依赖”实际回落到内置 provider。
2. 进入 Composite 页时 `_apply_composite_preview_color_space()` 会引用未定义的 `page_name`。
3. HDR 10-bit 视频导出仍会变暗，原因是 10-bit 码值被放进 16-bit RGB 容器却没有扩展到 16-bit full scale。
4. `timeline_clip_input` 路径仍把视频帧硬编码为 `srgb`，Rec.709/HDR 元数据没有进入 Composite IDT。
5. CPU 导出路径把 BGR 帧直接交给 RGB 语义的色域矩阵，宽色域导出颜色会错。

建议暂时不要继续第三阶段，也不要把这批改动当成稳定基线发布。先修 P0/P1 并补对应回归测试。

## 已执行验证

通过:

- `.venv/bin/python -m compileall -q src tests`
- `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/color/test_color_space_descriptor.py tests/color/test_hdr_transfer.py tests/composite/test_color_input_color_space.py tests/edit/test_comp_clip_settings.py -q`
- 结果: `74 passed in 1.47s`

额外烟测:

- OCIO config 加载烟测失败:
  - `OCIOColorProvider().is_available() == False`
  - 错误: `BuiltinTransform: invalid built-in transform style 'sRGB - EOTF'`
- HDR 10-bit RGB48/YUV420P10 roundtrip 烟测:
  - `uint16(1023)` 作为 `rgb48le` 白场输入，转 `yuv420p10le` 再转回 `rgb48le` 后最大值约 `1043`
  - `uint16(65535)` 作为 `rgb48le` 白场输入，转回后最大值约 `65506`
  - 结论: 当前 `1023` 缩放方案在 16-bit RGB 容器里接近黑，而不是 10-bit 白。

未重新跑全量 4892+ 测试基线。本轮重点是变更面静态审查、关键路径烟测和新增/相关测试验证。

## P0: 阻断级问题

### 1. OCIO config 当前无法加载

证据:

- `ocio/minimal.ocio:67-72` 使用 `!<BuiltinTransform> {style: sRGB - EOTF}`
- `ocio/minimal.ocio:87-90` 使用 `Rec.709 - EOTF`
- `ocio/minimal.ocio:106-128` 还使用了 PQ/HLG builtin style
- `src/shared/color/color_transform_provider.py:571-583` 加载失败后只打印 warning 并回退

影响:

- 第一阶段“OCIO 设为必需依赖”的目标没有真正达成。
- 当前运行时不会走 OCIO transform，直接退回内置 numpy provider。
- 测试没有覆盖 bundled config 是否能被当前 PyOpenColorIO 版本读取，所以这个问题会静默漏掉。

建议:

- 用当前依赖中的 PyOpenColorIO 查询/确认合法 BuiltinTransform style，或改用 `ExponentWithLinearTransform`/`LogCameraTransform`/显式 LUT/矩阵组合表达。
- 增加最小测试: 实例化 `OCIOColorProvider`，断言 bundled `minimal.ocio` 可加载，并对 `srgb -> linear`、`rec709 -> linear`、`bt2020_pq -> linear` 做 smoke transform。
- 如果 OCIO 真的必需，加载失败不应仅 warning 后回退，至少测试/启动诊断要明确失败。

### 2. Composite 页进入路径引用未定义变量 `page_name`

证据:

- `src/app/controllers/page_controller.py:358` 进入 Composite 页时调用 `_apply_composite_preview_color_space()`
- `src/app/controllers/page_controller.py:390-456` helper 内部 try/except 只包住色彩设置逻辑
- `src/app/controllers/page_controller.py:458-463` helper 尾部引用 `page_name`，但该函数没有这个局部变量或参数

影响:

- 当 `_composite_available` 且 `composite_viewer` 存在时，函数会在色彩设置之后继续执行到 `if page_name == "Color"`，触发 `NameError`。
- 这会破坏 Composite 页进入流程，也可能导致 preview ODT 初始化表现不稳定。

建议:

- 把 `Color page enter hook` 这段逻辑移回原本带 `page_name` 的页面切换函数，或给 helper 明确传入 `page_name`。从职责看，更合理的是移出 helper。
- 增加页面切换单测或至少一个无 GUI 的 smoke: 调用 Composite 进入 hook，不应抛异常，并验证 viewer 收到 `set_display_space()`。

### 3. HDR 10-bit 视频导出仍会近黑

证据:

- `src/features/composite/render/composite_render_output.py:379-388`
  - 注释说明 `CompositeFrameRenderer` 输出 BGR float32 scene-linear。
  - HDR 分支把编码结果乘以 `1023.0` 后转 `uint16`。
  - pixel format 标为 `bgr48le`。
- `src/shared/export/video_encoder.py:1067-1079`
  - `uint16` 或 `*48*` 格式会创建 `rgb48le` 临时帧。
  - PyAV/FFmpeg 对 `rgb48le` 期望的是 16-bit full-scale 样本，即白场约 `65535`，不是 `1023`。

影响:

- 之前复查发现的“8-bit 截断”已经改成了 16-bit 容器，但没有修正量程。
- 导出的 HDR10 视频会严重偏暗，白场只有约 1.6%。
- 这是用户可见的核心功能错误。

建议:

- 如果走 `rgb48le/bgr48le` 中间格式，应把编码后的 `[0, 1]` 扩展到 `65535` 再交给 PyAV，让 swscale 转到 `yuv420p10le`。
- 或者直接构造真正的 `yuv420p10le` 平面数据，但这需要正确处理矩阵、range 和 chroma subsampling。
- 增加测试: 白场/灰阶 HDR frame 经 `_frame_to_avframe` 或 `_convert_packed_frame` 转码后，YUV10 或 roundtrip RGB 亮度应接近预期。

### 4. Timeline clip 路径仍硬编码为 sRGB，视频元数据没有进入 Composite IDT

证据:

- `src/features/edit/core/timeline_clip_frame_provider.py:287-294` ColorOutput texture 返回 `color_space="srgb"`
- `src/features/edit/core/timeline_clip_frame_provider.py:355-365` 外部 GPU frame 路径返回 `color_space="srgb"`
- `src/features/edit/core/timeline_clip_frame_provider.py:679-686` CPU packed upload 返回 `color_space="srgb"`
- `src/features/edit/core/timeline_clip_frame_provider.py:892-897` planar GPU upload 返回 `color_space="srgb"`
- `src/features/composite/nodes/timeline_clip_input_node.py:121-126` 直接转发 provider 的 `TextureResult`
- `src/features/composite/evaluators/base_evaluator.py:1613-1644` IDT 依赖 `TextureResult.color_space` 和 `frame_metadata.color_primaries`

影响:

- 直接 `VideoFrameNode` 的 Rec.709/PQ/HLG 识别是正确方向，但 timeline/comp clip 入口没有享受到这套修复。
- 时间线视频进 Composite 时仍会被当成 sRGB 解码，Rec.709 gamma 差异和 HDR/PQ/HLG 都会丢失。
- 这会让“时间线源默认 Rec.709”和“HDR 源自动色调映射”的用户可见行为在重要路径上失效。

建议:

- `TimelineClipFrameProvider` 应从 asset/video session metadata 读取 `color_transfer`、`color_primaries`、`color_range`，映射到 `bt709`、`pq`、`hlg` 等 tag。
- `frame_metadata` 应携带 primaries/range，让 `_apply_idt_to_source_outputs()` 可以做正确 gamut decode。
- 增加测试: 构造带 `bt709`、`smpte2084` metadata 的 timeline clip input，断言 source output tag 和 IDT 调用 transfer/primaries 正确。

### 5. CPU 导出路径用 BGR 数据执行 RGB 语义色域矩阵

证据:

- `src/features/composite/render/composite_render_output.py:379-385` 明确 `frame_img` 是 BGR，但直接调用 `transform_color(frame_img, LINEAR_SCENE, output_descriptor)`
- `src/shared/color/color_transform_provider.py:399-435` 内置 provider 在 `_apply_primaries_np()` 中按 RGB 语义对 `img[..., :3]` 乘色域矩阵

影响:

- Rec.709/sRGB 这种只做逐通道 transfer 的场景影响较小。
- 但 BT.2020、Display P3 等宽色域输出需要矩阵转换，BGR 被当 RGB 会导致通道参与错误矩阵，颜色明显偏移。
- 这个 bug 和 HDR 导出同属 Output 渲染输出核心路径。

建议:

- 在 CPU render/export 调用 `transform_color()` 前后显式 BGR/RGB 转换，或让 `transform_color()` 接受 channel order 参数。
- 增加 BT.2020/P3 纯红、纯绿、纯蓝测试，确保 BGR 输入不会被错误矩阵转换。

## P1: 高风险问题

### 6. OCIO 宽色域矩阵方向和注释目标相反

证据:

- `ocio/minimal.ocio:91-113` 注释说 `bt2020_pq.to_reference` 应做 BT.2020 -> BT.709，但矩阵数值是 BT.709 -> BT.2020。
- `ocio/minimal.ocio:115-132` `bt2020_hlg` 同样问题。
- `ocio/minimal.ocio:134-152` 注释说 Display P3 -> BT.709，但矩阵数值是 BT.709 -> P3 D65。
- GPU shader 里同一路径使用了 inverse，方向是正确的: `src/features/composite/core/shaders.py:1660-1668`。

影响:

- 当前因为 OCIO config 加载失败，这个问题暂时是潜伏的。
- 一旦修好 builtin style，OCIO provider 的 BT.2020/P3 转换会和 GPU shader、内置 provider 不一致，宽色域颜色会错。

建议:

- 修 OCIO config 时同时修矩阵方向，或将 `from_reference`/`to_reference` 成对显式写清楚。
- 增加 provider 一致性测试: OCIO provider、builtin provider、GLSL shader 对同一组 RGB primaries 转换结果应在容差内一致。

### 7. HDR 自动预览色调映射检测不会触发

证据:

- `src/app/controllers/page_controller.py:423-430` 扫描 `video_frame` 和 `timeline_clip_input` node 的 `_cached_color_space`。
- 全仓库搜索只发现这个读取点，没有写入 `_cached_color_space` 的位置。

影响:

- 即使修复 `page_name` 的 `NameError`，HDR 图也不会把 `is_hdr_source` 置为 True。
- `tonemap` 仍保持 0，普通 SDR 屏预览 HDR 时不会自动走 Reinhard/ACES 色调映射。

建议:

- 对 `VideoFrameNode` 使用已经存在的检测结果或 metadata，而不是读取不存在的缓存字段。
- 对 `timeline_clip_input` 使用 provider 传出的 transfer metadata。
- 增加测试: PQ/HLG 源进入 Composite preview 时，viewer 收到 `tonemap=1` 和正确 target peak。

### 8. “bundle ICC”目前只有目录和 README，转换路径也没有优先使用 bundle

证据:

- `src/shared/color/profiles/` 目前只有 `README.md`，没有实际 `.icc/.icm` 文件。
- `src/features/color/core/color_manager.py:256-297` 的 `get_profile_for_space()` 会查 bundle 后返回 profile bytes。
- `src/features/color/core/color_manager.py:425-457` 的 `_convert_via_lcms()` 调 `_get_pil_profile()`。
- `src/features/color/core/color_manager.py:459-472` `_get_pil_profile()` 只查 sRGB 和 `self._system_profiles`，没有查 bundled profiles。

影响:

- “bundle ICC”在当前仓库中不是完整 bundle，只是用户手动放置目录。
- profile bytes/embedding 路径可能用到 bundle，但 LCMS 实际转换仍会绕开 bundle。
- CMYK/AdobeRGB/Display P3 如果系统 profile 不存在，仍可能静默回退 sRGB。

建议:

- 若目标是自包含 bundle，应提交实际可分发 profile，并在 license/README 中说明来源和授权。
- `_get_pil_profile()` 应复用同一套 lookup 顺序: bundled -> system -> sRGB fallback。
- 对非 sRGB 目标，如果 bundle/system 都缺失，应至少显式 warning，避免“静默 sRGB”。

## P2: 功能完整性和体验缺口

### 9. HDR/宽色域偏好键进了 config，但 UI 没有暴露

证据:

- `src/shared/config/config_manager.py:126-149` 有 primaries、HDR tonemap、HDR target peak、HDR metadata 默认键。
- `src/features/color/preferences.py:172-180` transfer combo 只包含 sRGB、Rec.709、Linear。
- `src/features/color/preferences.py:406-416` load 只读四个 transfer 键和 `color.smart_default`。
- `src/features/color/preferences.py:471-480` apply 只保存四个 transfer 键和 `color.smart_default`。

影响:

- 用户不能在偏好页选择 BT.2020、Display P3、PQ、HLG、HDR tonemap、target peak 或 HDR metadata。
- 第二阶段“新增 HDR/宽色域预设”和“偏好/智能预览”的可见 UI 还不完整。

建议:

- 用 `available_primaries()` 和扩展后的 `available_transfers()` 生成 UI。
- HDR 设置可在同组加入 tonemap mode、target peak、metadata toggle。
- 保存/恢复应覆盖 primaries/range/HDR keys。

### 10. `color.smart_default` 文档与实现不一致

证据:

- `src/shared/color/color_space_descriptor.py:240-242` docstring 说 `color.smart_default` 开启或 key 缺失时 fallback。
- `src/shared/color/color_space_descriptor.py:266-269` 实现只在 primaries/transfer 缺失时 fallback，没有读取 `color.smart_default`。
- `src/shared/config/config_manager.py:126-139` 默认配置总是带 primaries/transfer key。

影响:

- 用户打开 “Smart default” 不会覆盖已有配置键。
- 当前智能默认更像“默认值预置”，不是运行时 smart selection。

建议:

- 明确产品语义: 如果 smart default 是强制根据 source kind 选择，就在 `descriptor_from_config()` 读取并应用。
- 如果只是初始默认值，应改 UI 文案和 docstring，避免用户误解。

### 11. Display P3/ICC still image 自动识别还没接入 Composite source

证据:

- `src/features/composite/nodes/image_input_node.py:496-516` 只看显式 override、CPU hint、HDR 文件扩展，否则默认 sRGB。
- `src/features/composite/core/image_loader.py:168-182` 对普通图片只给 `color_space_hint = "srgb"`，没有读取 ICC 并映射 Display P3/Adobe RGB。

影响:

- 第二阶段加入 Display P3/ICC 相关能力，但普通 P3/Adobe RGB 图片进入 Composite 时仍大概率被当成 sRGB。
- 这会让宽色域 still image 的预览/导出链路不完整。

建议:

- image loader 读取 embedded ICC 或 ColorManager 检测结果，映射到 `ColorSpaceDescriptor`。
- 对无法映射的 ICC 至少保留 profile 名称并提示。

## 已确认相对健康的部分

- `ColorSpaceDescriptor` 的建模方向正确，能表达 sRGB 图片和 Rec.709 视频的 transfer/range 差异。
- `VideoFrameNode._detect_color_space()` 对直接视频文件节点可识别 `bt709`、PQ、HLG，默认视频回落到 `bt709` 的方向合理。
- `BaseEvaluator._apply_idt_to_source_outputs()` 对直接 source node 的 `srgb/bt709/pq/hlg` 转 scene-linear 的中心化设计合理。
- `_auto_align_color_spaces()` 已补充 `bt709`，作为 merge defensive fallback 是有效的。
- `CompositeViewport.restore_state()` 已恢复 `_display_transfer`，此前状态丢失问题看起来已修。
- `_bundled_profiles_dir()` 当前路径计算已指向 `src/shared/color/profiles/`，此前 `src/src/...` 的双重路径问题看起来已修。

## 建议修复顺序

1. 先修 `page_controller.py` 的 `page_name` NameError，恢复 Composite 页基本进入路径。
2. 修 OCIO config 加载失败，并补 `OCIOColorProvider` config-load 测试。
3. 修 HDR 10-bit 导出量程，补 PyAV/encoder roundtrip 测试。
4. 修 `TimelineClipFrameProvider` 的 color_space/metadata 传递，补 timeline clip Rec.709/PQ/HLG IDT 测试。
5. 修 CPU export 的 BGR/RGB 色域转换语义，补 BT.2020/P3 通道测试。
6. 修 OCIO 宽色域矩阵方向，并加 OCIO/builtin/GLSL 一致性测试。
7. 接入 HDR preview detection 的真实 metadata 来源。
8. 完成 bundle ICC 的实际 profile、LCMS 查找路径和缺失 profile warning。
9. 再补偏好页 HDR/宽色域 UI 和 `smart_default` 语义。

## 建议补充测试清单

- `tests/color/test_ocio_config_load.py`: bundled `minimal.ocio` 必须可加载，必要 colorspace transform 不可 silently fallback。
- `tests/export/test_hdr_10bit_video_encode.py`: 16-bit RGB 容器输入到 `yuv420p10le` 的白场/灰阶保持正确量程。
- `tests/composite/test_timeline_clip_color_metadata.py`: timeline clip source 根据 video metadata 输出 `bt709/pq/hlg` tag。
- `tests/composite/test_preview_color_space_apply.py`: Composite page preview apply 不抛异常，HDR 源传入 `tonemap`。
- `tests/color/test_provider_gamut_consistency.py`: OCIO、builtin、GLSL 对 BT.2020/P3 矩阵方向一致。
- `tests/color/test_color_manager_bundled_profiles.py`: LCMS 转换优先使用 bundled profile，缺失非 sRGB profile 时有明确 warning。

## 最终判断

第一阶段 MVP 的核心抽象和直接视频节点路径基本成立，但 timeline/comp clip 这条主路径仍有 sRGB 硬编码，不能算完全完成。

第二阶段 HDR/宽色域目前有数学和 shader 基础，但 OCIO config、HDR 导出量程、HDR preview 自动检测、bundle ICC 和偏好 UI 都存在关键缺口，不能算完成。

Output 节点渲染输出功能的 UI/worker/provider 结构已经接上，但一旦输出 HDR 或宽色域，当前导出链路会出现明显错误。建议先按上面的 P0/P1 修完并补测试，再决定是否推进第三阶段。
