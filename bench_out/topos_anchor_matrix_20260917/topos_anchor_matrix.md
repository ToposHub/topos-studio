# TOPOS 422Proxy 全链路速度锚点对比

## 测试口径

同一真实素材、同一分辨率、同一帧数、同一预热与进程隔离策略。
效果额外成本 = 效果总耗时 − 同组 identity 无效果总耗时；
不是把“强制 GPU”标签当成 GPU 证据，GPU 是否生效以 trail/fallback 为准。

| 层级 | FPS | p50/总耗时 | 说明 |
|---|---:|---:|---|
| 原生 TOPOS 解码 | 433.92 | 2.16ms | ok |
| 原生 TOPOS 编码（重复一张真实帧隔离编码器） | 147.17 | 6.48ms | ok |
| 真实逐帧 TOPOS 解码 → TOPOS 编码 | 96.57 | 0.497s | ok |

## TimelineExporter 锚点与效果

| 输出 | 场景 | 选路 | FPS | identity 基线 | 额外耗时 ms | 总耗时倍率 | GPU trail/fallback | 状态 |
|---|---|---|---:|---:|---:|---:|---|---|
| topos_proxy | identity | cpu_render | 9.598483687219945 | 9.598483687219945 | 0.0 | 1.000 | cpu_encoder_surface_path | ok |
| topos_proxy | identity | forced_gpu | 9.774692839283912 | 9.774692839283912 | 0.0 | 1.000 | cpu_encoder_surface_path | ok |
| topos_proxy | grade | cpu_render | 1.5953066083835226 | 9.598483687219945 | 25087.470121001388 | 6.017 | cpu_encoder_surface_path | ok |
| topos_proxy | grade | forced_gpu | 1.595726093559776 | 9.774692839283912 | 25169.710136000504 | 6.126 | cpu_encoder_surface_path | ok |
| topos_proxy | blur | cpu_render | 5.538630851636517 | 9.598483687219945 | 3665.6118600021728 | 1.733 | cpu_encoder_surface_path | ok |
| topos_proxy | blur | forced_gpu | 5.572742772100926 | 9.774692839283912 | 3702.7126379998663 | 1.754 | cpu_encoder_surface_path | ok |
| topos_proxy | sharpen | cpu_render | 4.035278922996096 | 9.598483687219945 | 6894.298651001009 | 2.379 | cpu_encoder_surface_path | ok |
| topos_proxy | sharpen | forced_gpu | 3.8016151360696995 | 9.774692839283912 | 7715.572114000679 | 2.571 | cpu_encoder_surface_path | ok |
| topos_proxy | glow | cpu_render | 4.047260828959583 | 9.598483687219945 | 6859.083270001065 | 2.372 | cpu_encoder_surface_path | ok |
| topos_proxy | glow | forced_gpu | 4.0557014532657645 | 9.774692839283912 | 6924.550429001101 | 2.410 | cpu_encoder_surface_path | ok |
| topos_proxy | two_layer | cpu_render | 2.9690178476546167 | 9.598483687219945 | 11166.172560000632 | 3.233 | cpu_encoder_surface_path | ok |
| topos_proxy | two_layer | forced_gpu | 3.0137099900174826 | 9.774692839283912 | 11016.57245000024 | 3.243 | cpu_encoder_surface_path | ok |
| topos_proxy | transition | cpu_render | 4.331918701932207 | 9.598483687219945 | 6079.750486000194 | 2.216 | cpu_encoder_surface_path | ok |
| topos_proxy | transition | forced_gpu | 4.397680063946926 | 9.774692839283912 | 6004.205610001009 | 2.223 | cpu_encoder_surface_path | ok |

## 解释与 GPU 优化门槛

- `identity` 是生产型无效果完整导出锚点，不等同于单纯解码速度；它包含时间线调度、帧交付、合成、编码和封装。
- `decode_encode` 是 TOPOS codec-only 锚点，用来区分素材解码/编码成本与编辑器效果链成本。
- `trail_fallback_reason=cpu_encoder_surface_path` 表示当前链路仍有 CPU 可见编码面；即使 GPU renderer 被请求，也不能算全链路 GPU。
- 下一轮优化必须优先让 identity 与无效果路径做到原生平面/Metal surface → GPU render → 硬件编码面，效果路径再逐项迁移。
