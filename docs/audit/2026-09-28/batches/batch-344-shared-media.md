# batch-344 审查报告：src/shared/media（色彩解码/元数据/解码策略）

审查日期：2026-09-28。编号区间：AUD-34400–AUD-34499。
审查维度：线程安全 / GPU 资源生命周期 / GLSL·HDR 正确性 / 信号防递归 / 状态提交一致性 / 异常与取消路径 / 输入验证 / 性能模式 / 类型注解缺失。

汇总：Critical 0 / Major 3 / Suggestion 9。

---

## src/shared/media/color_decode.py (行数: 975)

### 发现

- [AUD-34401] [Major] limited_to_full/full_to_limited 色度 offset 公式错误，中性色度被映射偏移约 4–9 LSB
  - 位置: src/shared/media/color_decode.py:632、src/shared/media/color_decode.py:677
  - 证据:
    ```python
    if is_luma:
        offset = constants['y_offset']
        max_val = constants['y_max']
    else:
        offset = constants['c_offset'] - constants['c_max'] / 2.0  # 色度中心
        max_val = constants['c_max']
    ```
  - 影响: 8-bit 时 offset = 128 − 240/2 = 8，而色度下限应为 c_min = 2×128 − 240 = 16。线性映射 full = (c−8)×255/232 使中性色度 128 → ≈131.9（正确 ≈127.5），低端 16 → ≈8.8（正确 0），最多偏移 ~9 LSB，产生整体色偏。`_apply_range_conversion`（P0-COLOR-06）已按 224 全幅修正，这两个 range 工具函数漏改（`c_offset - c_max/2` 求的是"半幅"而非对称下限）。当前 src/ 无调用方（color_transform_provider 使用独立实现 `_limited_to_full_np`），属潜伏缺陷；但模块 docstring（第 31 行）公开宣传该 API，一旦被采用即出错色。
  - 建议: offset 改为 `constants['c_offset'] - (constants['c_max'] - constants['c_offset'])`（或直接引入 c_min 常量），并补与 `_apply_range_conversion` 对拍的单测（128→127.5 等锚点）。
  - 置信度: 高

- [AUD-34402] [Suggestion] 未收录色彩矩阵静默回退 BT.709，错色无告警
  - 位置: src/shared/media/color_decode.py:484、src/shared/media/color_decode.py:936、src/shared/media/color_decode.py:955
  - 证据:
    ```python
    conv_matrix = self.MATRIX_LUT.get(matrix, self.BT709_MATRIX)
    ...
    return coefficients.get(matrix, coefficients[ColorMatrix.BT709])
    ...
    return id_map.get(matrix, 0)
    ```
  - 影响: MATRIX_LUT 仅含 BT709/BT2020_NC/BT601/BT601_PAL 四项，而 ColorMatrix 还有 FCC、SMPTE240M、BT2020_C、YCGCO、ICTCP、GBR、UNKNOWN；传入即静默按 709 系数转换，与项目"不得靠猜测解释色彩语义"（ToposColorMetadataError 的设计意图）相悖。已核对现有调用方（async_renderer.py:4688、video_frame_node.py:1135、video_playback.py:3884、window_provider.py:290、topos_source.py:2201）均先收敛到 3 矩阵且 topos_source 对未知 matrix 有 W1 告警，故降为 Suggestion。
  - 建议: 对未收录矩阵 raise ToposColorMetadataError 或至少 logger.warning 一次（与 topos_frame_to_rgb 的 W1 姿态一致）。
  - 置信度: 高

- [AUD-34403] [Suggestion] linear_to_hlg 对负输入无钳制，NaN 可穿透 np.clip 进入显示链
  - 位置: src/shared/media/color_decode.py:809-815
  - 证据:
    ```python
    low_region = data <= 1.0
    high_region = data > 1.0

    hlg = np.zeros_like(data)

    # Low region: sqrt
    hlg[low_region] = np.sqrt(3.0 * data[low_region])
    ```
  - 影响: low_region 包含负值（预乘 alpha 反解、浮点噪声可产生微小负数），sqrt(负) → NaN，而 np.clip 对 NaN 不生效（仍为 NaN），最终输出 NaN 污染渲染。同类的 linear_to_pq（746 行）、linear_to_srgb（858 行）入口均有 np.clip，唯独 linear_to_hlg 缺失。
  - 建议: 入口加 `data = np.clip(np.asarray(data, dtype=np.float32), 0.0, None)`（HLG 场景线性上限由调用方策略决定，只钳下限即可）。
  - 置信度: 高（代码事实确定；是否触发取决于调用方输入）

- [AUD-34404] [Suggestion] tonemap_pq_to_srgb 存在死变量 scale_factor
  - 位置: src/shared/media/color_decode.py:903
  - 证据:
    ```python
    # 简化的 Reinhard tone mapping: L_out = L_in / (1 + L_in)
    # 或使用更保守的映射
    scale_factor = target_peak / 1000.0  # 假设 HDR 参考值为 1000 nits

    # Normalize and tone map
    normalized = linear_nits / target_peak
    ```
  - 影响: scale_factor 赋值后从未使用；注释宣称的"假设 HDR 参考值为 1000 nits"逻辑实际未生效，易误导后续维护者以为存在参考白缩放。
  - 建议: 删除死变量或落实其用途。
  - 置信度: 高

- [AUD-34405] [Suggestion] resolve 接受的 transfer 集合与 ColorConverter 可解码集合不对称，错误推迟到转换期
  - 位置: src/shared/media/color_decode.py:86-94、src/shared/media/color_decode.py:407、src/shared/media/color_decode.py:427
  - 证据:
    ```python
    _TOPOS_TRANSFERS = {
        1: ColorTransfer.BT709, 4: ColorTransfer.GAMMA22,
        5: ColorTransfer.GAMMA28, 6: ColorTransfer.SMPTE170M,
        7: ColorTransfer.SMPTE240M, 8: ColorTransfer.LINEAR,
        ...
    raise ToposColorMetadataError(f"unsupported transfer decode: {transfer.value}")
    ```
  - 影响: resolve_topos_color_semantics 接受 GAMMA22/28、SMPTE170M/240M、BT2020_10/12，但 decode_transfer_to_linear / linear_to_display 对这些 transfer 一律 raise。文件打开（元数据解析）成功、到实际取帧转换时才抛异常，错误暴露点远离源头。硬错误本身符合设计原则，问题仅在两个集合不同步。
  - 建议: 在 resolve 阶段即校验 transfer 可解码性，或为缺失曲线补 CPU 实现，保持两个集合同步。
  - 置信度: 高

- [AUD-34406] [Suggestion] chroma_siting 未做取值范围校验；模块存在未使用导入 Any
  - 位置: src/shared/media/color_decode.py:159、src/shared/media/color_decode.py:42
  - 证据:
    ```python
    chroma_siting = integer("chroma_siting") if "chroma_siting" in idsc else 0
    ```
    ```python
    from typing import Tuple, Dict, Any, Optional, Mapping
    ```
  - 影响: chroma_siting 接受任意 int（含负数、超大值），后续作为 GLSL siting 参数使用时可能越界；`Any` 在全文件无使用点，属冗余导入。
  - 建议: 校验 chroma_siting ∈ {0,1,2,3}（H.273 语义），超界 raise；删除 Any 导入。
  - 置信度: 高

### 通过项
- 线程安全：ColorConverter 无实例可变状态（常量为类级不可变 ndarray，仅读），单例 get_color_converter 的懒初始化竞争为良性（幂等、无状态）。
- GPU 资源生命周期：纯 CPU NumPy 模块，不持有 ModernGL/纹理/缓冲资源。
- HDR 正确性：yuv_to_rgb 明确不 clamp 保留高光（507-508 行）；decode_to_display 仅在显式显示边界 clip 并要求 PQ 降 SDR 必须提供 pq_display_peak_nits（447-450 行），符合"内部数据不得无理由 clamp"铁律；PQ 常量 c2=2413/128、c3=2392/128（345-349 行注释纠正了旧值）；HLG 正/逆 OETF 分段公式与 ARIB STD-B67 一致；TRAW_LOG0 逆映射与 cfa_cpu_debayer 常量同源（391-406 行，已核对常量存在）。
- 信号防递归：无 Qt 信号/控件，不适用。
- 状态提交一致性：无 undo/redo/revision 状态。
- 异常路径：无宽泛 except；ToposColorMetadataError 硬错误替代猜测（resolve_topos_color_semantics 的 CICP 冲突、ICCP 校验、OCIO 名称/path 校验完整）。
- 输入验证：IDSC 字段类型/枚举域校验完整；ICC profile 长度/acsp 签名/色彩空间校验完整；premultiply/unpremultiply 形状校验存在且 alpha=0 保护正确。
- 性能：向量化 NumPy（np.dot/np.power/布尔索引），_match_chroma_to_luma 用 repeat+pad 处理奇数尺寸色度面，无明显热点。

---

## src/shared/media/color_metadata.py (行数: 888)

### 发现

- [AUD-34421] [Major] 枚举映射表缺 TRAW_LOG0/ST2084 条目，ffmpeg→enum→ffmpeg 往返丢失 transfer 标签
  - 位置: src/shared/media/color_metadata.py:76-94、src/shared/media/color_metadata.py:144-156、src/shared/media/color_metadata.py:201-215
  - 证据:
    ```python
    _FFMPEG_COLOR_TRANSFER_MAP: Dict[int, ColorTransfer] = {
        ...
        18: ColorTransfer.HLG,
    }
    ```
    ```python
    _ENUM_TO_FFMPEG_TRANSFER: Dict[ColorTransfer, int] = {
        ...
        ColorTransfer.PQ: 16,
        ColorTransfer.HLG: 18,
    }
    ```
    而兼容表已补：`20: 'traw-log0',   # RC7：TRAW PWL-log12`（507 行）；src/shared/color/types.py:93 定义 `TRAW_LOG0 = 'topos-traw-log0'`，src/shared/media/color_decode.py:93 `_TOPOS_TRANSFERS[20]` 亦已收录。
  - 影响: 三套表不同步——`ffmpeg_to_color_transfer(20)` 返回 UNKNOWN（而 `normalize_color_transfer(20)` 返回 'traw-log0'），TRAW 源经枚举路径读取时 transfer 被静默降级，后续 G3-b 式显示变换选错曲线；`color_transfer_to_ffmpeg(ColorTransfer.TRAW_LOG0)` 与 `color_transfer_to_ffmpeg(ColorTransfer.ST2084)` 返回 None（ST2084 在 _ENUM_TO_FFMPEG_TRANSFER 同样缺失），导出/打标签时元数据丢失。`_TRANSFER_TO_STANDARD_NAME` 也无 TRAW_LOG0，get_standard_name 返回 'unknown'。
  - 建议: 在 _FFMPEG_COLOR_TRANSFER_MAP 补 `20: ColorTransfer.TRAW_LOG0`；_ENUM_TO_FFMPEG_TRANSFER 补 `TRAW_LOG0: 20` 与 `ST2084: 16`；_TRANSFER_TO_STANDARD_NAME 补 `TRAW_LOG0: 'traw-log0'`；加往返一致性单测（对每个枚举成员断言 to_ffmpeg/get_standard_name 非 None）。
  - 置信度: 高（代码事实确定；对 TRAW 源的实际影响取决于读路径走枚举表还是兼容表，标中高）

- [AUD-34422] [Suggestion] normalize_color_* 对未知字符串返回原值而非 'unknown'，违反自身 docstring 契约
  - 位置: src/shared/media/color_metadata.py:588-591、src/shared/media/color_metadata.py:680、src/shared/media/color_metadata.py:741、src/shared/media/color_metadata.py:792
  - 证据:
    ```python
    Returns:
        标准名称（如 'smpte2084', 'arib-std-b67', 'bt709'）
        或 'unknown'（无法识别）
        或 None（输入为 None）
    ...
    # 未知的字符串，返回原值（规范化后）
    return name_lower
    ```
  - 影响: `_normalize_transfer_alias('foobar')` → 'foobar'、`_normalize_primaries_alias`/`_normalize_space_alias` 同样兜底返回 name_lower。下游若以 `== 'unknown'` 判断失败，任意垃圾字符串会被当作有效标准名传给 FFmpeg/存储层，与文档承诺不符。
  - 建议: 三个 alias 函数未知分支改为返回 'unknown'，或修改 docstring 明示透传行为并让调用方校验。
  - 置信度: 高

- [AUD-34423] [Suggestion] 标准名/反向映射缺 AP1 条目；get_standard_name 参数缺类型注解
  - 位置: src/shared/media/color_metadata.py:185-198、src/shared/media/color_metadata.py:129-141、src/shared/media/color_metadata.py:436
  - 证据:
    ```python
    def get_standard_name(color_enum) -> str:
    ```
    _PRIMARIES_TO_STANDARD_NAME（185-198 行）与 _ENUM_TO_FFMPEG_PRIMARIES（129-141 行）均无 `ColorPrimaries.AP1` 条目，而 src/shared/color/types.py:56 已定义 `AP1 = 'ap1'`（内部工作空间 tag）。
  - 影响: `get_standard_name(ColorPrimaries.AP1)` 返回 'unknown'，AP1 素材在显示名/UI 层被标为未知；`get_standard_name(color_enum)` 参数无类型注解，与模块"类型安全转换"设计原则不符（本批仅此一处公开函数签名缺注解）。
  - 建议: 两个 primaries 表补 AP1（FFmpeg 反向可保持 None 但标准名应为 'ap1'）；参数注解为 `ColorPrimaries | ColorTransfer | ColorMatrix | ColorRange`。
  - 置信度: 高

### 通过项
- 线程安全：纯模块级只读映射表 + 无状态函数。
- GPU 资源生命周期：不涉及。
- GLSL·HDR 正确性：映射值与 ISO/IEC 23091-1 / ITU H.273 逐项核对一致（primaries 1/4/5/6/7/8/9/10/11/12/22；transfer 1/4/5/6/7/8/13/14/15/16/18，9/10/11/12/17 显式降级 UNKNOWN；matrix 0/1/4/5/6/7/8/9/10/14；range 0/1/2）；BT601→'smpte170m'、BT601_PAL→'bt470bg' 的标准名对应正确。
- 信号防递归：无 Qt 信号，不适用。
- 状态提交一致性：无状态。
- 异常路径：无 except 吞噬；enum→int 失败均以受控 (ValueError, TypeError) 捕获后走 'unknown'。
- 输入验证：None 输入统一返回 None/UNKNOWN，int/str/enum 多形态输入均有处理分支。
- 性能：字典直查，无循环扫描。
- 类型注解：除 get_standard_name 外全部完整。

---

## src/shared/media/decode_policy.py (行数: 571)

### 发现

- [AUD-34411] [Major] _get_8bit_format 递归降级未实现（pass 占位），预览实际返回 'yuv422p' 而非文档承诺的 'yuv420p'
  - 位置: src/shared/media/decode_policy.py:343-346、src/shared/media/decode_policy.py:41-44、src/shared/media/decode_policy.py:185
  - 证据:
    ```python
    # 直接查找降级映射
    if source_format in DecodePolicyManager._DOWNGRADE_MAP:
        target = DecodePolicyManager._DOWNGRADE_MAP[source_format]
        # 递归查找直到找到 8-bit 格式
        if target in DecodePolicyManager._DOWNGRADE_MAP:
            # 检查是否需要进一步降级
            pass
        return target
    ```
    模块头示例（41-44 行）：
    ```python
    output_fmt = DecodePolicyManager.get_output_format('yuv422p10le', preview_policy)
    print(output_fmt)  # 'yuv420p'
    ```
  - 影响: PREVIEW 策略 force_8bit=True 时 get_output_format 走 `_get_8bit_format`：'yuv422p10le' → 'yuv422p'（文档承诺 'yuv420p'），'yuv444p10le' → 'yuv444p'。_DOWNGRADE_MAP 中的 'yuv444p'→'yuv422p'、'yuv422p'→'yuv420p' 链（244-245 行）从该路径不可达，PREVIEW.preferred_format='yuv420p'（185 行）的策略意图未生效——预览保留 4:2:2/4:4:4 子采样，带宽与解码开销高于设计目标，与 4K 实时性能基准冲突，且文档行为与实现不符。
  - 建议: 实现循环降级：`while target in _DOWNGRADE_MAP and 需继续: target = _DOWNGRADE_MAP[target]`（或按 policy.preferred_format 直接返回 'yuv420p'），并补上模块头示例对应的单测。
  - 置信度: 高

- [AUD-34412] [Suggestion] get_output_format 首分支语义颠倒且近乎死代码
  - 位置: src/shared/media/decode_policy.py:285-287
  - 证据:
    ```python
    # 如果策略指定了首选格式且不允许降级，直接返回首选格式
    if policy.preferred_format and not policy.allow_downgrade:
        return policy.preferred_format
    ```
  - 影响: "不允许降级"时强制返回 preferred_format 本身相对源格式往往就是降级/改格式（语义自相矛盾）；内置三个策略无一命中该分支（PREVIEW allow_downgrade=True，MASTER/EXPORT preferred_format=None），属易误用的死分支。
  - 建议: 删除该分支，或改为 `if not policy.allow_downgrade: return source_format` 的正确保真语义。
  - 置信度: 高

- [AUD-34413] [Suggestion] get_export_format 中 base_format 计算后未使用（死变量）
  - 位置: src/shared/media/decode_policy.py:484-491
  - 证据:
    ```python
    # 提取基础格式（yuv420p, yuv422p, yuv444p 等）
    for pattern in ['420p', '422p', '444p', '420', '422', '444']:
        if pattern in source_format:
            base_format = f"yuv{pattern}p" if not pattern.endswith('p') else f"yuv{pattern}"
            break
    ```
  - 影响: base_format 赋值后从未读取，后续直接用 pattern 重构格式名；空循环匹配失败时 base_format 仍为 source_format 亦无消费，徒增误导。
  - 建议: 删除 base_format 相关代码（保留 pattern 提取即可）。
  - 置信度: 高

### 通过项
- 线程安全：_POLICIES/_DOWNGRADE_MAP/_RGB_FORMAT_MAP 为类级只读表，DecodePolicy 为 frozen dataclass，get_policy 返回共享实例不可被调用方篡改。
- GPU 资源生命周期：不涉及（硬件加速仅为策略开关，资源管理在解码器层）。
- GLSL·HDR 正确性：MASTER/EXPORT 不做位深/子采样降级（allow_downgrade=False、max_bit_depth=None），符合母版保真原则；预览降级仅作用于码值格式，无 HDR clamp 语义。
- 信号防递归：无 Qt 信号，不适用。
- 状态提交一致性：无状态对象，无 undo/revision 参与。
- 异常与取消路径：全静态方法无异常吞噬，未知格式保持原样返回（fail-safe）。
- 输入验证：_infer_bit_depth 对 10/12/14/16/f32/48/64 位模式与 '10be' 等字节序后缀覆盖正确（已逐项核对常见 FFmpeg 格式名）。
- 性能：字典/字符串直查，无热点。
- 类型注解：完整（含 `int | None`、`tuple[int, int] | None`）。

---

## 汇总

| 严重度 | 数量 | 编号 |
|--------|------|------|
| Critical | 0 | — |
| Major | 3 | AUD-34401、AUD-34411、AUD-34421 |
| Suggestion | 9 | AUD-34402、34403、34404、34405、34406、34412、34413、34422、34423 |

批次总体：三个文件均为纯 CPU/纯数据模块，无 GPU 资源、无 Qt 信号、无 undo 状态参与；主要风险集中在色彩数值正确性（色度 offset 公式、矩阵静默回退、NaN 穿透）与元数据映射表不同步（TRAW_LOG0/ST2084/AP1 缺项），以及解码策略文档与实现不符（预览降级未落地）。
