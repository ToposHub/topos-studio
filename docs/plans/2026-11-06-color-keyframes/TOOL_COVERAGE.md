# 工具覆盖清单（KF-01 生成）

由 `src/features/color/core/keyframe_tracks.py::render_coverage_markdown()` 生成——**勿手改**，改动先落在
`DOMAIN_CAPABILITIES` / 各 `*_TRACK_FIELDS` 注册表，再重新生成（tests/color/test_keyframe_tracks.py 强制同步）。

## 域能力汇总

| 域 | 写入域 | read | validate | write | evaluate | preview | deliver | copy | 未支持原因 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Grade 调色参数 | graph+layer | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| Window 窗口子轨 | graph+layer | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| Qualifier 限定器 | graph+layer | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| Sizing | — | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | read: 槽位身份（Input/Node/Output Sizing、Convergence 等）仅存在于面板演示数据；后端尚未定义（KF-03 落槽位域）、validate: 同上——无槽位注册表可校验、write: 槽位→坐标系归属待 KF-00 OP-16 实测后接命令、evaluate: 无后端轨道可求值、preview: 无后端轨道可求值、deliver: 无后端轨道可求值、copy: 无后端轨道可复制 |
| Open FX / 独立 FX | graph+layer | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 条件支持：仅已注册 DCTL 实例的 slider/float 连续标量；按实例 schema 校验。Color/GL 逐帧求值，Deliver 使用 OpenGL；CPU/Metal 原生 FX、离散/颜色参数及 AI 效果尚未支持。 |

## 字段登记（逐行往返/拒绝用例见 tests/color/test_keyframe_tracks.py）

### Grade 调色参数（grade）

| 字段 | 值形态 | 环形 | 备注 |
| --- | --- | --- | --- |
| lift | vec3 | — | — |
| gamma | vec3 | — | — |
| gain | vec3 | — | — |
| exposure | scalar | — | — |
| temp | scalar | — | temperature 兼容双名 |
| temperature | scalar | — | temp 兼容双名 |
| tint | scalar | — | — |
| contrast | scalar | — | — |
| shadow | scalar | — | shadows 兼容旧名（v1 归一） |
| highlights | scalar | — | — |
| saturation | scalar | — | — |
| vibrance | scalar | — | — |
| mid_detail | scalar | — | — |
| sharpness | scalar | — | — |
| hue_shift | scalar | — | — |
| lift_saturation | scalar | — | — |
| gamma_saturation | scalar | — | — |
| gain_saturation | scalar | — | — |

### Window 窗口子轨（window）

| 字段 | 值形态 | 环形 | 备注 |
| --- | --- | --- | --- |
| center | vec2 | — | — |
| size | vec2 | — | — |
| rotation | scalar | — | — |
| feather | scalar | — | — |
| opacity | scalar | — | — |
| blur | scalar | — | — |
| clean_black | scalar | — | — |
| clean_white | scalar | — | — |
| softness | scalar | — | — |

### Qualifier 限定器（qualifier）

| 字段 | 值形态 | 环形 | 备注 |
| --- | --- | --- | --- |
| hue_center | scalar | 是 | — |
| hue_width | scalar | — | — |
| hue_soft | scalar | — | — |
| sat_low | scalar | — | — |
| sat_high | scalar | — | — |
| sat_soft_low | scalar | — | — |
| sat_soft_high | scalar | — | — |
| lum_low | scalar | — | — |
| lum_high | scalar | — | — |
| lum_soft_low | scalar | — | — |
| lum_soft_high | scalar | — | — |

PENDING-KF00：动态权重非 (1,1)/(0,0) 档位与 Dissolve 滑杆档位映射待 Resolve 实测（KF00_RESOLVE_FIXTURES.md）。
