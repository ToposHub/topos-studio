# TOPOS 422Proxy 全链路速度锚点对比

## 测试口径

同一真实素材、同一分辨率、同一帧数、同一预热与进程隔离策略。
效果额外成本 = 效果总耗时 − 同组 identity 无效果总耗时；
不是把“强制 GPU”标签当成 GPU 证据，GPU 是否生效以 trail/fallback 为准。

| 层级 | FPS | p50/总耗时 | 说明 |
|---|---:|---:|---|
| 原生 TOPOS 解码 | 415.06 | 2.28ms | ok |
| 原生 TOPOS 编码（重复一张真实帧隔离编码器） | 159.33 | 6.29ms | ok |
| 真实逐帧 TOPOS 解码 → TOPOS 编码 | 101.45 | 0.473s | ok |

## TimelineExporter 锚点与效果

| 输出 | 场景 | 选路 | FPS | identity 基线 | 额外耗时 ms | 总耗时倍率 | GPU trail/fallback | 状态 |
|---|---|---|---:|---:|---:|---:|---|---|
| topos_proxy | identity | cpu_render | 91.36542817171367 | 91.36542817171367 | 0.0 | 1.000 | topos_direct_planar_cpu_encode | ok |
| topos_proxy | identity | forced_gpu | 83.57977614066675 | 83.57977614066675 | 0.0 | 1.000 | topos_direct_planar_cpu_encode | ok |
| topos_proxy | grade | cpu_render | 1.6040469945272633 | 91.36542817171367 | 29398.9474689879 | 56.959 | cpu_encoder_surface_path | ok |
| topos_proxy | grade | forced_gpu | 36.61722479278683 | 83.57977614066675 | 736.5568800159963 | 2.283 | cpu_encoder_surface_path | ok |
| topos_proxy | blur | cpu_render | 5.153611473619313 | 91.36542817171367 | 8788.49411100964 | 17.728 | cpu_encoder_surface_path | ok |
| topos_proxy | blur | forced_gpu | 35.80442895869642 | 83.57977614066675 | 766.3146600098116 | 2.334 | cpu_encoder_surface_path | ok |
| topos_proxy | sharpen | cpu_render | 3.7120939129895145 | 91.36542817171367 | 12405.344506987603 | 24.613 | cpu_encoder_surface_path | ok |
| topos_proxy | sharpen | forced_gpu | 36.13740416784572 | 83.57977614066675 | 753.9620300085517 | 2.313 | cpu_encoder_surface_path | ok |
| topos_proxy | glow | cpu_render | 3.578580699919586 | 91.36542817171367 | 12887.776070987456 | 25.531 | cpu_encoder_surface_path | ok |
| topos_proxy | glow | forced_gpu | 32.484214069244416 | 83.57977614066675 | 903.3391880220734 | 2.573 | cpu_encoder_surface_path | ok |
| topos_proxy | two_layer | cpu_render | 2.776271104927964 | 91.36542817171367 | 16764.014966000104 | 32.909 | cpu_encoder_surface_path | ok |
| topos_proxy | two_layer | forced_gpu | 30.620398692193728 | 83.57977614066675 | 993.2808530138573 | 2.730 | cpu_encoder_surface_path | ok |
| topos_proxy | transition | cpu_render | 4.121758356197851 | 91.36542817171367 | 11120.152468996821 | 22.167 | cpu_encoder_surface_path | ok |
| topos_proxy | transition | forced_gpu | 32.151212898783356 | 83.57977614066675 | 918.6436220043106 | 2.600 | cpu_encoder_surface_path | ok |

## 解释与 GPU 优化门槛

- `identity` 是生产型无效果完整导出锚点，不等同于单纯解码速度；它包含时间线调度、帧交付、合成、编码和封装。
- `decode_encode` 是 TOPOS codec-only 锚点，用来区分素材解码/编码成本与编辑器效果链成本。
- `trail_fallback_reason=cpu_encoder_surface_path` 表示当前链路仍有 CPU 可见编码面；即使 GPU renderer 被请求，也不能算全链路 GPU。
- 下一轮优化必须优先让 identity 与无效果路径做到原生平面/Metal surface → GPU render → 硬件编码面，效果路径再逐项迁移。
