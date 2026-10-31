# TOPOS 422Proxy 全链路速度锚点对比

## 测试口径

同一真实素材、同一分辨率、同一帧数、同一预热与进程隔离策略。
效果额外成本 = 效果总耗时 − 同组 identity 无效果总耗时；
不是把“强制 GPU”标签当成 GPU 证据，GPU 是否生效以 trail/fallback 为准。

| 层级 | FPS | p50/总耗时 | 说明 |
|---|---:|---:|---|
| 原生 TOPOS 解码 | 409.67 | 2.35ms | ok |
| 原生 TOPOS 编码（重复一张真实帧隔离编码器） | 132.70 | 6.83ms | ok |
| 真实逐帧 TOPOS 解码 → TOPOS 编码 | 90.22 | 0.532s | ok |

## TimelineExporter 锚点与效果

| 输出 | 场景 | 选路 | FPS | identity 基线 | 额外耗时 ms | 总耗时倍率 | GPU trail/fallback | 状态 |
|---|---|---|---:|---:|---:|---:|---|---|
| topos_proxy | identity | cpu_render | 80.04662075288928 | 80.04662075288928 | 0.0 | 1.000 | full_gpu/unknown | ok |
| topos_proxy | identity | forced_gpu | 72.04936321655362 | 72.04936321655362 | 0.0 | 1.000 | full_gpu/unknown | ok |
| topos_proxy | grade | cpu_render | 1.5050467568365922 | 80.04662075288928 | 31293.046328002674 | 53.185 | cpu_encoder_surface_path | ok |
| topos_proxy | grade | forced_gpu | 1.554701459780745 | 72.04936321655362 | 30207.8847230041 | 46.343 | cpu_encoder_surface_path | ok |
| topos_proxy | blur | cpu_render | 5.284242290322068 | 80.04662075288928 | 8483.96018800122 | 15.148 | cpu_encoder_surface_path | ok |
| topos_proxy | blur | forced_gpu | 5.339686867139025 | 72.04936321655362 | 8323.081256003206 | 13.493 | cpu_encoder_surface_path | ok |
| topos_proxy | sharpen | cpu_render | 3.9998135696898074 | 80.04662075288928 | 11400.908768999216 | 20.013 | cpu_encoder_surface_path | ok |
| topos_proxy | sharpen | forced_gpu | 4.1565409601707595 | 72.04936321655362 | 10881.853840000986 | 17.334 | cpu_encoder_surface_path | ok |
| topos_proxy | glow | cpu_render | 3.962819610489045 | 80.04662075288928 | 11512.937134000822 | 20.199 | cpu_encoder_surface_path | ok |
| topos_proxy | glow | forced_gpu | 3.852442833751669 | 72.04936321655362 | 11793.416893004178 | 18.702 | cpu_encoder_surface_path | ok |
| topos_proxy | two_layer | cpu_render | 2.8935160491714247 | 80.04662075288928 | 15989.163608999661 | 27.664 | cpu_encoder_surface_path | ok |
| topos_proxy | two_layer | forced_gpu | 2.855800964376405 | 72.04936321655362 | 16141.684124002495 | 25.229 | cpu_encoder_surface_path | ok |
| topos_proxy | transition | cpu_render | 4.182738427335132 | 80.04662075288928 | 10876.084986000933 | 19.137 | cpu_encoder_surface_path | ok |
| topos_proxy | transition | forced_gpu | 4.044785518567239 | 72.04936321655362 | 11200.921182004095 | 17.813 | cpu_encoder_surface_path | ok |

## 解释与 GPU 优化门槛

- `identity` 是生产型无效果完整导出锚点，不等同于单纯解码速度；它包含时间线调度、帧交付、合成、编码和封装。
- `decode_encode` 是 TOPOS codec-only 锚点，用来区分素材解码/编码成本与编辑器效果链成本。
- `trail_fallback_reason=cpu_encoder_surface_path` 表示当前链路仍有 CPU 可见编码面；即使 GPU renderer 被请求，也不能算全链路 GPU。
- 下一轮优化必须优先让 identity 与无效果路径做到原生平面/Metal surface → GPU render → 硬件编码面，效果路径再逐项迁移。
