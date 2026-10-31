# Topos 视频 V1：共享色彩、Alpha 与容器时间语义

状态：已实现并通过 `tests/media/test_topos_source.py` 的 native-backed 回归。

V1 不复制一套视频颜色转换规则。`ToposMediaSource` 在打开 MOV 时把轨级
`tpcC` 配置映射为 `idsc` 字段，调用与 `.toos` 相同的
`resolve_topos_color_semantics()`。因此 TPIC 的 pixel format、bit depth、range、
matrix、primaries、transfer 和 chroma siting 的合法性由同一处参考实现决定；MOV
当前没有独立 ICC/OCIO chunk，所以显示权威记录为 `CICP`，而不会静默猜测 sRGB。

当前语义边界：

| 项目 | 当前 all-intra MOV | 验证方式 |
| --- | --- | --- |
| 数值重建 | tpcC 与每个 TPIC frame header 交叉校验 | native mux/decode + source open |
| Alpha | straight/premultiplied 字段沿用 frame config；输出平面保持声明位深满刻度 | `topos_alpha_premultiplied` 与 Alpha 回归 |
| 时间基 | `timebase = 1/timescale`；`duration_ticks = last_pts + last_duration` | source metadata |
| 帧率 | `timescale * sample_count / duration_ticks`，常规恒定帧时精确约分 | `Rational` |
| 元数据继承 | 容器 tpcC 是默认值；frame header 不一致由 native reader 拒绝 | MOV header consistency tests |
| 外部显示 | 8-bit preview 仍只是显示边界量化，不改变源位深或 CICP 解释 | `topos_frame_to_rgb()` / shared color tests |

VFR 语义不再用 `total_frames / nominal_fps` 猜时长。`VideoMetadata.duration_seconds`
优先使用容器时间基和精确 tick 数；播放器仍可使用 `fps` 作为平均帧率提示，逐帧
播放和 seek 使用 sample 的 PTS/duration。

未在本阶段宣称支持的能力：MOV 内独立 ICC/OCIO 继承块、音频轨、旋转/编辑列表
重写、P/B 帧和 DPB。它们分别由 O8/V8 生态、V7 容器或后续 V3 产品线处理。
