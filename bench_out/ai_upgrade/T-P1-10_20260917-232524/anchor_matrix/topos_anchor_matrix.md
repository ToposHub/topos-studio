# TOPOS 422Proxy 全链路速度锚点对比

## 测试口径

同一真实素材、同一分辨率、同一帧数、同一预热与进程隔离策略。
效果额外成本 = 效果总耗时 − 同组 identity 无效果总耗时；
不是把“强制 GPU”标签当成 GPU 证据，GPU 是否生效以 trail/fallback 为准。

| 层级 | FPS | p50/总耗时 | 说明 |
|---|---:|---:|---|
| 原生 TOPOS 解码 | 391.67 | 2.49ms | ok |
| 原生 TOPOS 编码（重复一张真实帧隔离编码器） | 124.01 | 7.97ms | ok |
| 真实逐帧 TOPOS 解码 → TOPOS 编码 | 87.16 | 0.551s | ok |

## TimelineExporter 锚点与效果

| 输出 | 场景 | 选路 | FPS | identity 基线 | 额外耗时 ms | 总耗时倍率 | GPU trail/fallback | 状态 |
|---|---|---|---:|---:|---:|---:|---|---|
| topos_proxy | identity | cpu_render | 10.147942000785198 | 10.147942000785198 | 0.0 | 1.000 | cpu_encoder_surface_path | ok |
| topos_proxy | identity | forced_gpu | 9.985155196664293 | 9.985155196664293 | 0.0 | 1.000 | cpu_encoder_surface_path | ok |
| topos_proxy | grade | cpu_render | 1.576348802636382 | 10.147942000785198 | 25720.0904359961 | 6.438 | cpu_encoder_surface_path | ok |
| topos_proxy | grade | forced_gpu | 1.4994093917776483 | 9.985155196664293 | 27205.468505999306 | 6.659 | cpu_encoder_surface_path | ok |
| topos_proxy | blur | cpu_render | 5.4894083771215305 | 10.147942000785198 | 4014.088604999415 | 1.849 | cpu_encoder_surface_path | ok |
| topos_proxy | blur | forced_gpu | 5.5409248064523675 | 9.985155196664293 | 3855.6777230005537 | 1.802 | cpu_encoder_surface_path | ok |
| topos_proxy | sharpen | cpu_render | 3.7007424666687165 | 10.147942000785198 | 8240.347160000965 | 2.742 | cpu_encoder_surface_path | ok |
| topos_proxy | sharpen | forced_gpu | 3.826119236103618 | 9.985155196664293 | 7738.212604999717 | 2.610 | cpu_encoder_surface_path | ok |
| topos_proxy | glow | cpu_render | 3.9611420677661964 | 10.147942000785198 | 7387.694267999905 | 2.562 | cpu_encoder_surface_path | ok |
| topos_proxy | glow | forced_gpu | 4.235520207473368 | 9.985155196664293 | 6525.59226700032 | 2.357 | cpu_encoder_surface_path | ok |
| topos_proxy | two_layer | cpu_render | 3.1074415964858506 | 10.147942000785198 | 10716.767622998304 | 3.266 | cpu_encoder_surface_path | ok |
| topos_proxy | two_layer | forced_gpu | 3.014389842717531 | 9.985155196664293 | 11116.484436002793 | 3.312 | cpu_encoder_surface_path | ok |
| topos_proxy | transition | cpu_render | 4.470381202955185 | 10.147942000785198 | 6007.316258001083 | 2.270 | cpu_encoder_surface_path | ok |
| topos_proxy | transition | forced_gpu | 4.4117553013014295 | 9.985155196664293 | 6072.88709400018 | 2.263 | cpu_encoder_surface_path | ok |

## 解释与 GPU 优化门槛

- `identity` 是生产型无效果完整导出锚点，不等同于单纯解码速度；它包含时间线调度、帧交付、合成、编码和封装。
- `decode_encode` 是 TOPOS codec-only 锚点，用来区分素材解码/编码成本与编辑器效果链成本。
- `trail_fallback_reason=cpu_encoder_surface_path` 表示当前链路仍有 CPU 可见编码面；即使 GPU renderer 被请求，也不能算全链路 GPU。
- 下一轮优化必须优先让 identity 与无效果路径做到原生平面/Metal surface → GPU render → 硬件编码面，效果路径再逐项迁移。
