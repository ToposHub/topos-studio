# Topos Color 色彩管理系统当前状态审查与升级方案

日期：2026-06-28

分支/提交：`fix-ui` / `36849f8b fix(color): 修复色彩系统审查发现的 P0/P1 问题`

## 1. 结论

这轮优化完成度明显提升，之前审查中最危险的静默失效点已经基本被补上：

- bundled `ocio/minimal.ocio` 现在可以被 OCIO 正常加载。
- Composite 进入页时的 `page_name` 未定义崩溃已移出 `_apply_composite_preview_color_space()`。
- HDR 10-bit 导出从 `1023` 缩放修为 `65535` 缩放，避免 PyAV/Swscale 把 HDR 白场压到近黑。
- Composite Render 输出前已做 BGR/RGB 翻转，宽色域矩阵不再直接套在 BGR 语义上。
- Timeline clip raw source 不再一律硬标 `srgb`，Rec.709/PQ/HLG transfer 标签已开始从视频元数据传递。
- `_bundled_profiles_dir()` 当前指向 `src/shared/color/profiles/`，路径不再是 `src/src/...`。

对日常任务而言，当前系统可以认为是“SDR 日常可用、HDR/宽色域试用可用、专业闭环尚未完全可用”。

可放心覆盖的日常场景：

- sRGB 图片合成、预览、普通 PNG/JPEG/TIFF/EXR 输出。
- 常规 Rec.709 SDR 视频素材进入 Composite，不再被粗暴当成 sRGB。
- Output 节点后台渲染输出视频或序列的主体功能。
- HDR10/HLG 的基础数学、shader 编译、HDR 10-bit 编码路径和静态 HDR 元数据注入。

仍不建议承诺“完全可用/专业闭环”的场景：

- Rec.709 在 OCIO、builtin、GPU shader 之间仍有曲线定义不一致风险。
- BT.2020 SDR、Display P3 still image、ICC-managed still image 还没有贯穿 source -> IDT -> preview -> export。
- Timeline planar YUV 上传没有根据源元数据设置 BT.2020/BT.601/BT.709 matrix 与 full/limited range。
- bundle ICC 目录没有实际 profile 文件，LCMS 转换函数也还没有优先使用 bundled profile。
- 偏好 UI 仍主要只暴露 transfer，未完整暴露 primaries/range/HDR tone-map/HDR metadata。

## 2. 已执行验证

通过：

```bash
.venv/bin/python -m compileall -q src tests
```

结果：通过，仅有两个既有 `canvas.py` tooltip 转义 `SyntaxWarning`。

通过：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color/test_color_space_descriptor.py \
  tests/color/test_hdr_transfer.py \
  tests/color/test_ocio_config_load.py \
  tests/composite/test_hdr_10bit_video_encode.py \
  tests/composite/test_timeline_clip_color_metadata.py \
  tests/composite/test_color_input_color_space.py \
  tests/edit/test_comp_clip_settings.py -q
```

结果：`88 passed in 1.20s`。

通过：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/video/test_ffmpeg_encoder.py \
  tests/video/test_yuv_upload_api.py \
  tests/media/test_color_metadata_mapping.py \
  tests/media/test_color_convert.py -q
```

结果：`356 passed in 1.86s`，仅 `color_decode.py` PQ overflow RuntimeWarning，测试本身通过。

通过：

```bash
git diff --check
```

结果：无 whitespace error。

## 3. 当前残留问题

### P1: Rec.709 曲线在 OCIO 与 GPU/builtin 之间不一致

现象：

- `src/features/composite/core/shaders.py` 使用 BT.709 OETF/inverse OETF：`E = 4.5*L` / `1.099*L^0.45 - 0.099`。
- `src/shared/color/color_transform_provider.py` 的 builtin 路径同样使用 BT.709 OETF/inverse OETF。
- `ocio/minimal.ocio` 的 `rec709` 当前使用 `ExponentTransform {value: [2.4, ...]}`，也就是更接近 BT.1886/display gamma 的纯 pow 曲线。
- 默认 `composite.color_engine` 是 `ocio`，因此 CPU 导出和 GPU 预览可能对同一 Rec.709 中间调给出不同结果。

复现数值：

```text
OCIO REC709_VIDEO -> LINEAR_SCENE, code 0.5:   ~0.1979
builtin REC709_VIDEO -> LINEAR_SCENE, code 0.5: ~0.2685
raw BT.709 inverse OETF, code 0.5:              ~0.2596
```

影响：

- Rec.709 SDR 视频导出与 viewer 预览不完全一致。
- 用户切换 color engine 后，同一项目可能出现中间调差异。
- 当前测试只验证“能 roundtrip/不是 no-op”，没有验证 CPU/GPU/OCIO 曲线一致性。

实现方法：

1. 先明确项目内 Rec.709 contract：
   - 推荐：encoded Rec.709 media 的 IDT/ODT 统一使用 BT.709 OETF/inverse OETF，因为现有 GPU shader 与 builtin 已经按这个方向实现。
   - BT.1886 应作为 display EOTF 或独立 view transform，而不是混入 media decode/encode descriptor。
2. 让 OCIO 路径与 contract 对齐：
   - 方案 A：为 minimal OCIO 生成 `rec709_to_linear.spi1d` / `linear_to_rec709.spi1d` 1D LUT，并在 config 中用 `FileTransform` 表达 BT.709 piecewise 曲线。
   - 方案 B：对 app 内置 descriptor 的 `bt709` 变换不走 OCIO，统一走 builtin 数学；OCIO 只负责外部 config/camera/log/ACES spaces。
   - 方案 C：新增 `ColorTransfer.BT1886`，把纯 pow 2.4 独立出来，避免 `bt709` 名称承载两种含义。
3. 增加 provider parity 测试：
   - `tests/color/test_color_transform_provider_parity.py`
   - 对 sRGB、Rec.709、PQ、HLG、BT.2020 gamut 做 `OCIOColorProvider` vs `BuiltinColorProvider` 的样本比较。
   - Rec.709 样本：`0.0, 0.018, 0.081, 0.18, 0.5, 0.9, 1.0`。

验证方法：

- 单测要求 Rec.709 `OCIOColorProvider.transform(x, REC709_VIDEO, LINEAR_SCENE)` 与 builtin 误差 `< 1e-3`。
- GPU shader 用离屏 1xN texture 渲染同一组样本，与 CPU reference 误差 `< 2e-3`。
- 导出 18% gray Rec.709 视频，`ffmpeg` 抽帧后反解码，灰阶误差在容忍范围内。

### P1: 用户选择的 color engine 没有贯穿所有导出路径

现象：

- `src/features/composite/render/composite_render_output.py` 中视频输出调用 `transform_color(rgb_linear, LINEAR_SCENE, output_descriptor)`，没有传 `config_manager`。
- 同文件 `_encode_linear_to_srgb()` 直接 `get_color_provider()`，也没有传 `config_manager`。
- `src/features/composite/nodes/file_output_node.py` 也直接 `get_color_provider()`。

影响：

- 用户在偏好页选择 `builtin` 时，部分路径仍会默认选择 OCIO。
- 如果 Rec.709 OCIO/builtin 曲线不一致，这个问题会变成可见的导出差异。

实现方法：

1. `CompositeRenderOutput` 增加 `config_manager` 成员或在 render context 中传入 `color_provider`。
2. `CompositeRenderWorker.run()` 从 context 传递 `config_snapshot` 或 `color_engine`/`ocio_config_path`。
3. 所有 `transform_color()` / `get_color_provider()` 调用点统一传 `config_manager` 或显式 provider。
4. `FileOutputNode` 在 evaluation context 中读取 provider，而不是直接全局默认。

验证方法：

- 新增测试：设置 fake config `composite.color_engine=builtin`，monkeypatch `OCIOColorProvider`，确认 Composite video/still/file_output 不会创建 OCIO provider。
- 设置 `composite.color_engine=ocio`，确认同一路径会使用 OCIO provider。

### P1: Timeline 宽色域 SDR primaries 仍可能丢失

现象：

- `TimelineClipFrameProvider` 已将 `frame_metadata` 写成 dict：`{"color_primaries": source_prim_tag, ...}`。
- `BaseEvaluator._apply_idt_to_source_outputs()` 读取时使用 `getattr(meta, "color_primaries", None)`。
- dict 没有 `color_primaries` 属性，因此 BT.2020 SDR 或 Display P3 SDR timeline source 会退回 BT.709 primaries。
- PQ/HLG HDR 因为代码强制 `primaries = "bt2020"`，不受这个具体 bug 影响。

影响：

- BT.2020 SDR、P3 SDR 视频合成会有 gamut 错误。
- 单测只测了 `_detect_source_color_space()` helper，没有覆盖 evaluator 真实读取 dict metadata。

实现方法：

1. 新增工具函数：

```python
def _metadata_get(meta: Any, key: str, default: Any = None) -> Any:
    if isinstance(meta, dict):
        return meta.get(key, default)
    return getattr(meta, key, default)
```

2. `BaseEvaluator._apply_idt_to_source_outputs()` 使用 `_metadata_get()`。
3. 更推荐：给 `TextureResult` 增加 `source_descriptor: Optional[ColorSpaceDescriptor]`，让 evaluator 不再拼 transfer/primaries 字符串。

验证方法：

- 新增 evaluator 单测：构造 `TextureResult(color_space="bt709", frame_metadata={"color_primaries": "bt2020"})`，mock `_convert_to_linear()`，断言收到 `primaries="bt2020"`。
- 再测 object-style metadata，避免兼容性回退。

### P1: Timeline planar YUV 上传没有根据源元数据设置 matrix/range

现象：

- `YUVGPUUploader` 支持 `set_yuv_matrix()` 和 `set_color_range()`，默认是 BT.709 + limited。
- `TimelineClipFrameProvider._upload_planar_frame()` 在调用 `render_nv12_to_rgb()` / `render_yuv_to_rgb()` 前没有根据源 `color_matrix` / `color_range` 设置 uploader。

影响：

- BT.2020 P010 HDR 源在 YUV -> RGB 阶段可能先被 BT.709 matrix 解码，之后再做 PQ/BT.2020 IDT 已经来不及完全修复。
- BT.601 SD 素材、full-range 视频也可能出现色相/对比度偏差。

实现方法：

1. `_detect_source_color_space()` 扩展返回：

```python
ColorSourceTags(
    transfer_tag: str,
    primaries_tag: str,
    matrix_tag: str,
    range_tag: str,
    bit_depth: int,
)
```

2. 在 `_upload_planar_frame()` 前设置：

```python
if matrix_tag in ("bt2020", "bt2020nc"):
    uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT2020)
elif matrix_tag in ("bt601", "smpte170m"):
    uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT601)
else:
    uploader.set_yuv_matrix(YUVGPUUploader.MATRIX_BT709)

uploader.set_color_range(
    YUVGPUUploader.RANGE_FULL if range_tag in ("full", "pc", "jpeg")
    else YUVGPUUploader.RANGE_LIMITED
)
```

3. 将 matrix/range 一并写入 `frame_metadata`。

验证方法：

- 用 mock uploader 测 `_upload_planar_frame()` 前确实调用 `set_yuv_matrix(BT2020)` / `set_color_range(FULL)`。
- 用 BT.2020 synthetic YUV patch 做 CPU reference 与 GPU readback 比较。
- 覆盖 NV12/P010/YUV420/YUV444 四类路径。

### P1: Composite Render still/image-sequence 输出未完整使用 output descriptor

现象：

- `render_video()` 已根据 `output_descriptor` 编码。
- `_convert_for_image_ext()` 仍固定 `_encode_linear_to_srgb()`，没有使用 `color.output.image_*` 或 `color.output.timeline_*` descriptor。
- image sequence 走 `render_image_sequence()` 时未传 output descriptor。

影响：

- 用户设置输出为 Rec.709、Display P3 或 HDR still/sequence 时，Composite Render 仍可能按 sRGB 输出。
- EXR 保持 linear 是对的，但 PNG/JPEG/TIFF/sequence 缺少 descriptor 与 ICC embedding。

实现方法：

1. 修改 `_convert_for_image_ext(img, ext, output_descriptor, config_manager)`。
2. `CompositeRenderWorker` 对 image sequence 也传 `output_descriptor`。
3. still image render 根据 graph source 类型选择 `kind="image"` 或 `kind="timeline"`。
4. PNG/JPEG/TIFF 输出后调用 `ColorManager.embed_icc()`，ICC profile 与 output descriptor 对齐。

验证方法：

- image sequence 设置 `color.output.image_transfer=bt709`，导出 PNG，抽样确认像素走 BT.709 OETF 而不是 sRGB。
- Display P3 TIFF/JPEG 输出检查 ICC profile 名称。
- EXR 输出确认不被 OETF 编码，线性值保持 >1.0。

### P1: ICC bundle 仍不自包含，LCMS 转换未优先使用 bundled profile

现象：

- `src/shared/color/profiles/` 目前只有 `README.md`，没有实际 `.icc/.icm`。
- `ColorManager.get_profile_for_space()` 会优先查 bundled profile。
- 但 `_get_pil_profile()` 仍只查 sRGB in-process 与 system profile，没有复用 `get_profile_for_space()` 的 bundled bytes。

影响：

- 本机有系统 profile 时看起来能用，换机器后仍可能退回 sRGB。
- CMYK/Display P3/Adobe RGB 不能算真正 bundle 自包含。

实现方法：

1. 根据许可证决定是否随 app 分发 profile：
   - sRGB、Rec.2020、Display P3 可优先使用可再分发来源。
   - Adobe RGB、SWOP/FOGRA39 需要确认 EULA，必要时提供下载/安装脚本而不是直接 commit。
2. `_get_pil_profile()` 改为：

```python
icc = self.get_profile_for_space(color_space)
if icc:
    return ImageCms.ImageCmsProfile(BytesIO(icc))
```

3. 系统扫描只作为 fallback，不作为 conversion 主路径。
4. 增加 profile health check，在偏好页或启动日志中提示缺失 profile。

验证方法：

- 临时清空 `_system_profiles`，放入 bundled DisplayP3.icc，断言 `_get_pil_profile("Display P3")` 使用 bundled。
- 对一张 Display P3 红色样本做 LCMS -> sRGB，确认结果发生合理 gamut 映射。
- 缺失 profile 时给出 warning，不静默伪装成目标 profile 转换成功。

### P2: 偏好 UI 与配置能力不匹配

现象：

- `ConfigManager` 已有 primaries/range/HDR 相关 key。
- `src/features/color/preferences.py` 目前主要暴露 4 个 transfer combo 和 `smart_default`。
- `descriptor_from_config()` 文档说 smart_default on 会 fallback，但实现只在 key 缺失时 fallback；默认配置里 key 总是存在。

影响：

- 用户看不到/改不了 primaries、range、HDR tonemap、HDR target peak、HDR metadata。
- “智能默认”开关的语义不清晰，可能只是一个保存的布尔值，而不是实际策略开关。

实现方法：

1. `descriptor_from_config()` 明确模式：
   - `color.smart_default=True`：优先 source metadata；metadata 缺失时 video -> Rec.709、image -> sRGB。
   - `color.smart_default=False`：严格使用用户手动配置。
2. 偏好页新增：
   - Preview / Output tabs。
   - Timeline / Image 子区。
   - Primaries combo：BT.709 / Display P3 / BT.2020。
   - Transfer combo：sRGB / Rec.709 / Linear / PQ / HLG。
   - Range segmented control：Full / Limited。
   - HDR tonemap combo：Off / Reinhard / ACES。
   - HDR target peak numeric input。
   - HDR metadata checkbox。
3. UI 遵循现有偏好页风格，不新增营销式说明；用 tooltip 做细节解释。

验证方法：

- QTest 加载默认配置，检查 UI 初始值。
- 修改每个控件并 apply，检查 config key。
- 切换 smart_default 后验证 `descriptor_from_config()` 输出。

### P2: Still image ICC/P3 源检测未进入 Composite IDT

现象：

- `image_loader.py` 普通图片只按扩展名给 `linear` 或 `srgb` hint。
- `ImageInputNode._detect_color_space()` 只使用 override、decode hint、扩展名。
- 嵌入 ICC 的 Display P3/AdobeRGB 图片不会自动映射到 `display_p3` 或相应 descriptor。

影响：

- 宽色域手机照片、Display P3 PNG/JPEG 在 Composite 中仍可能当 sRGB。
- 色彩系统的 ICC 模块与 Composite 源节点仍是两套世界。

实现方法：

1. `image_loader.decode_image()` 读取 `icc_profile` bytes。
2. 用 `ColorManager.identify_color_space()` 映射：
   - Display P3 -> `display_p3`
   - Adobe RGB -> 新增 descriptor 或 ICC conversion path
   - unknown -> `srgb` + warning metadata
3. `DecodedImage` 增加 `icc_profile_bytes`、`detected_color_space_name`、`source_descriptor`。
4. `ImageInputNode` 输出 `TextureResult.source_descriptor` 或 `frame_metadata`。
5. 对没有 GPU shader descriptor 支持的 ICC space，先在 CPU decode 阶段 LCMS 转到 working profile。

验证方法：

- 生成带 Display P3 ICC 的 PNG，导入后断言 node 输出 `display_p3`。
- Composite IDT mock `_convert_to_linear()`，断言 primaries 为 P3。
- 与 PIL/LCMS reference 比较一组 P3 色块。

### P2: HDR preview 检测依赖 last-evaluated node tag

现象：

- `page_controller._apply_composite_preview_color_space()` 对 `timeline_clip_input` 读 `_last_source_color_tag`。
- 该值需要 node evaluation 后才会写入。

影响：

- 第一次进入 HDR timeline composite 时，可能先按 SDR ODT 设置，首帧之后才具备 HDR tag。
- 如果 graph 未立即 evaluation，tonemap 状态可能滞后一帧或一次页面切换。

实现方法：

1. Auto-bind timeline clip 时预读 source metadata，并写入 runtime context。
2. `TimelineClipInputNode` 初始化/绑定时直接保存 `source_descriptor_hint`。
3. `_apply_composite_preview_color_space()` 优先读 runtime context/source descriptor，再读 last-evaluated fallback。
4. 当 evaluator 发现 HDR source，发出 metadata-ready signal，viewer 重新 set_display_space。

验证方法：

- mock HDR runtime context，进入 Composite 前不触发 node evaluate，断言 viewer tonemap 已打开。
- 首帧 evaluate 后 tonemap 不反复抖动。

### P2: HDR metadata 仍是静态 preset，未从源或内容分析得出

现象：

- HDR10 输出用 `ColorMetadataConfig.hdr10()` 的标准 1000nit mastering/MaxCLL。
- 没有从源视频 ST2086/MaxCLL/MaxFALL 继承，也没有分析合成后内容峰值。

影响：

- 普通 HDR10 能被播放器识别，但 metadata 不一定代表真实内容。
- 专业交付时可能需要准确 mastering display 与 MaxCLL/MaxFALL。

实现方法：

1. VideoMetadata 增加 `mastering_display`、`max_cll`、`max_fall` 字段映射。
2. source metadata 进入 `CompositeRuntimeContext`。
3. 合成输出阶段：
   - 若只有一个 HDR source 且无强烈改变，可继承源 metadata。
   - 若有多个 source 或调色/合成改变，按导出帧抽样/全量扫描计算 MaxCLL/MaxFALL。
4. UI 提供：
   - Use source metadata
   - Use project default
   - Analyze on export
   - Custom

验证方法：

- ffprobe 检查导出 HEVC `mastering display metadata` 与 `Content Light Level Metadata`。
- 合成白场 1000nits patch，分析输出 MaxCLL 接近 1000。

## 4. 推荐升级路线

### 阶段 A：先建立统一色彩 contract（最高优先级）

目标：让 GPU preview、CPU export、OCIO/builtin provider 对同一 descriptor 给出一致结果。

实施步骤：

1. 编写 `docs/color_transform_contract.md`，明确：
   - 工作空间：BT.709 primaries / scene-linear / full range。
   - sRGB still：BT.709 primaries / IEC 61966-2-1 / full。
   - Rec.709 video：BT.709 primaries / BT.709 OETF / limited。
   - BT.1886：只作为 display EOTF/view transform，不等同于 Rec.709 encoded media。
   - PQ linear：以 nits 为单位。
   - HLG linear：relative scene light。
2. 修正 OCIO/builtin/GPU 的 Rec.709 parity。
3. 所有 `get_color_provider()` 调用点传递 config 或显式 provider。
4. 新增 provider parity + shader parity 测试。

验收标准：

- Rec.709 midtone CPU/GPU/OCIO 差异小于 `1e-3`。
- sRGB/Rec.709/PQ/HLG roundtrip 全部稳定。
- 用户选择 builtin/OCIO 时，导出路径实际使用对应 provider。

### 阶段 B：把 descriptor 作为唯一元数据货币贯穿 Composite

目标：不再靠散落的 `color_space` 字符串和 dict metadata 猜测。

实施步骤：

1. `TextureResult` / `_TexResult` 增加：

```python
source_descriptor: Optional[ColorSpaceDescriptor]
color_matrix: Optional[str]
bit_depth: Optional[int]
icc_profile_bytes: Optional[bytes]
```

2. `TimelineClipFrameProvider` 从 `VideoMetadata` 构建 descriptor。
3. `ImageInputNode` 从 ICC/extension/user override 构建 descriptor。
4. `BaseEvaluator._apply_idt_to_source_outputs()` 优先使用 `source_descriptor`。
5. 保留旧 `color_space` 字符串做兼容 fallback。

验收标准：

- Rec.709、BT.2020 SDR、PQ、HLG、Display P3 source 都能在 evaluator 中得到正确 transfer/primaries。
- merge auto-align 不再依赖几个硬编码字符串。

### 阶段 C：修完整 YUV planar 元数据路径

目标：YUV -> RGB 的 matrix/range 与后续 IDT 使用同一份源元数据。

实施步骤：

1. 扩展 `_detect_source_color_space()` 为 `ColorSourceTags` dataclass。
2. `_upload_planar_frame()` 每次 render 前设置 uploader matrix/range。
3. 对 CPU packed frame 标记是否已经由 FFmpeg/PyAV 完成 range/matrix 转换，避免重复处理。
4. 将 matrix/range 写入 `frame_metadata` 和 `source_descriptor`。

验收标准：

- BT.2020 P010 HDR synthetic frame GPU readback 与 CPU reference 色差在阈值内。
- BT.601 SD、BT.709 HD、BT.2020 HDR 三套 matrix 都被测试覆盖。

### 阶段 D：补齐 ICC 与 still image 工作流

目标：现代手机照片/Display P3/Adobe RGB/CMYK 导入导出不再伪装成 sRGB。

实施步骤：

1. 完成 bundled profile 策略：
   - 可分发 profile 直接放入 `src/shared/color/profiles/`。
   - 不可直接分发 profile 提供 installer/checker。
2. `_get_pil_profile()` 复用 bundled bytes。
3. `image_loader.py` 读取 ICC 并返回 source descriptor / ICC bytes。
4. Composite IDT 支持 Display P3 still；AdobeRGB/ProPhoto 先通过 LCMS 转 working。
5. 导出 PNG/JPEG/TIFF 时根据 output descriptor embedding ICC。

验收标准：

- 无系统 profile 环境下，bundle profile 测试仍通过。
- Display P3 PNG 导入、预览、导出 roundtrip 有 golden delta。
- CMYK 输出至少能通过 LCMS 转换并嵌入目标 profile；若暂不支持 4 通道导出，UI 明确禁用而不是静默 sRGB。

### 阶段 E：完善 HDR 预览与交付

目标：HDR 不只是“能打开/能导出”，而是具备可解释的预览和元数据策略。

实施步骤：

1. HDR source descriptor 在 page enter 前可用，避免 tonemap 首帧滞后。
2. Viewer 增加 HDR debug overlay：
   - transfer
   - primaries
   - tonemap
   - target peak
   - source peak/max sampled value
3. HDR output metadata 支持 source/custom/analyze。
4. 分离 SDR tone-map export 与 HDR passthrough export。
5. 添加 `hdr_target_peak` 和 `tone_map_operator` 到 render settings 快照。

验收标准：

- HDR10 source 在 SDR monitor 上首帧不过曝。
- HDR10 -> HDR10 导出保持 10-bit、PQ、BT.2020、ST2086/MaxCLL。
- HDR10 -> SDR 导出走明确 tone-map，输出 Rec.709/sRGB metadata 正确。

## 5. 测试矩阵建议

新增/扩展测试文件：

- `tests/color/test_color_transform_provider_parity.py`
- `tests/color/test_color_contract_numeric_values.py`
- `tests/composite/test_source_descriptor_propagation.py`
- `tests/composite/test_composite_render_output_color_descriptor.py`
- `tests/composite/test_hdr_preview_detection.py`
- `tests/color/test_icc_bundle_resolution.py`
- `tests/color/test_image_icc_detection.py`
- `tests/video/test_timeline_planar_yuv_color_metadata.py`

建议 golden/material：

- sRGB PNG/JPEG：灰阶、肤色、饱和彩色条。
- Rec.709 limited H.264：18% gray、75% color bars。
- Full-range H.264：确认 range 不被二次压缩。
- BT.2020 SDR HEVC：验证 gamut。
- HDR10 PQ P010：100/400/1000/4000 nits patch。
- HLG BT.2020：broadcast-style ramp。
- Display P3 PNG/JPEG with embedded ICC。
- Adobe RGB TIFF with embedded ICC。

数值验收：

- sRGB 0.5 -> linear：`0.2140 ± 0.002`。
- BT.709 inverse OETF 0.5 -> linear：`0.2596 ± 0.002`，若 contract 改为 BT.1886，则所有实现必须统一到同一数值。
- Limited range black/white：8-bit code 16/235 对应 full linear decode边界正确。
- PQ 10000nits -> code 1.0；PQ 100nits/1000nits 样本 roundtrip 误差小于 `1e-3` relative。
- GPU shader vs CPU reference：LDR `< 2e-3`，HDR tone-map `< 5e-3`。

端到端验收：

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest \
  tests/color \
  tests/composite \
  tests/video/test_ffmpeg_encoder.py \
  tests/video/test_yuv_upload_api.py \
  tests/media/test_color_metadata_mapping.py \
  tests/media/test_color_convert.py
```

导出文件验收：

```bash
ffprobe -hide_banner -show_streams -show_frames -select_streams v:0 output.mp4
```

检查：

- `color_primaries`
- `color_transfer`
- `color_space`
- `color_range`
- HDR10 `mastering display metadata`
- HDR10 `Content Light Level Metadata`

## 6. 发布前 checklist

- [ ] OCIO/builtin/GPU 对同一 descriptor 数值一致。
- [ ] 所有导出路径尊重用户选择的 color engine。
- [ ] `TextureResult` 有统一 source descriptor，旧 `color_space` 仅作 fallback。
- [ ] Timeline planar YUV 根据 matrix/range 元数据上传。
- [ ] Composite still/image-sequence 输出使用 output descriptor。
- [ ] Display P3/ICC still image 能被识别并正确进入 IDT。
- [ ] bundled ICC 策略明确：实际文件、安装脚本或显式缺失提示。
- [ ] HDR preview 首帧自动 tone-map，不依赖上一次 evaluation 状态。
- [ ] HDR metadata 支持 source/custom/analyze 至少一种非静态模式。
- [ ] 新增 parity/golden/ffprobe 测试进入 CI 或本地 release gate。

## 7. 推荐下一步

下一轮不要直接扩 Dolby Vision/ACES/CMYK 4 通道。优先做阶段 A-C：

1. 统一 Rec.709 contract 与 provider 选择。
2. 用 `ColorSpaceDescriptor` 贯穿 `TextureResult`。
3. 修 Timeline planar YUV matrix/range。

这三项完成后，当前色彩系统才算从“功能可用”进入“结果可信”。之后再补 ICC still workflow 和 HDR metadata，会更稳。
