# TOPOS 422Proxy 全链路速度锚点对比

## 测试口径

同一真实素材、同一分辨率、同一帧数、同一预热与进程隔离策略。
效果额外成本 = 效果总耗时 − 同组 identity 无效果总耗时；
不是把“强制 GPU”标签当成 GPU 证据，GPU 是否生效以 trail/fallback 为准。

| 层级 | FPS | p50/总耗时 | 说明 |
|---|---:|---:|---|
| 原生 TOPOS 解码 | 399.82 | 2.34ms | ok |
| 原生 TOPOS 编码（重复一张真实帧隔离编码器） | 153.36 | 6.55ms | ok |
| 真实逐帧 TOPOS 解码 → TOPOS 编码 | 97.20 | 0.494s | ok |

## TimelineExporter 锚点与效果

| 输出 | 场景 | 选路 | FPS | identity 基线 | 额外耗时 ms | 总耗时倍率 | GPU trail/fallback | 状态 |
|---|---|---|---:|---:|---:|---:|---|---|
| topos_proxy | identity | cpu_render | 88.17206567034748 | 88.17206567034748 | 0.0 | 1.000 | topos_direct_planar_cpu_encode | ok |
| topos_proxy | identity | forced_gpu | 86.53677916837236 | 86.53677916837236 | 0.0 | 1.000 | topos_direct_planar_cpu_encode | ok |
| topos_proxy | grade | cpu_render | 1.4451337626315004 | 88.17206567034748 | 32670.528294001997 | 61.013 | cpu_encoder_surface_path | ok |
| topos_proxy | grade | forced_gpu | 33.47141152946425 | 86.53677916837236 | 879.3821790022776 | 2.585 | cpu_encoder_surface_path | ok |
| topos_proxy | blur | cpu_render | 4.648269023877889 | 88.17206567034748 | 9782.034583004133 | 18.969 | cpu_encoder_surface_path | ok |
| topos_proxy | blur | forced_gpu | 32.927870398927276 | 86.53677916837236 | 903.0542350010364 | 2.628 | cpu_encoder_surface_path | ok |
| topos_proxy | sharpen | cpu_render | 3.5414040200979704 | 88.17206567034748 | 13009.556220000377 | 24.897 | cpu_encoder_surface_path | ok |
| topos_proxy | sharpen | forced_gpu | 33.80271737391742 | 86.53677916837236 | 865.3267310000956 | 2.560 | cpu_encoder_surface_path | ok |
| topos_proxy | glow | cpu_render | 3.4082594775049317 | 88.17206567034748 | 13539.044657001796 | 25.870 | cpu_encoder_surface_path | ok |
| topos_proxy | glow | forced_gpu | 32.45800429557582 | 86.53677916837236 | 924.1565400079708 | 2.666 | cpu_encoder_surface_path | ok |
| topos_proxy | two_layer | cpu_render | 2.6269501260097 | 88.17206567034748 | 17727.74971500621 | 33.564 | cpu_encoder_surface_path | ok |
| topos_proxy | two_layer | forced_gpu | 29.21551426845756 | 86.53677916837236 | 1088.2852430077037 | 2.962 | cpu_encoder_surface_path | ok |
| topos_proxy | transition | cpu_render | 3.7295599065511587 | 88.17206567034748 | 12325.761120999232 | 23.641 | cpu_encoder_surface_path | ok |
| topos_proxy | transition | forced_gpu | 30.729521077498685 | 86.53677916837236 | 1007.3384330025874 | 2.816 | cpu_encoder_surface_path | ok |

## 解释与 GPU 优化门槛

- `identity` 是生产型无效果完整导出锚点，不等同于单纯解码速度；它包含时间线调度、帧交付、合成、编码和封装。
- `decode_encode` 是 TOPOS codec-only 锚点，用来区分素材解码/编码成本与编辑器效果链成本。
- `trail_fallback_reason=cpu_encoder_surface_path` 表示当前链路仍有 CPU 可见编码面；即使 GPU renderer 被请求，也不能算全链路 GPU。
- 下一轮优化必须优先让 identity 与无效果路径做到原生平面/Metal surface → GPU render → 硬件编码面，效果路径再逐项迁移。
