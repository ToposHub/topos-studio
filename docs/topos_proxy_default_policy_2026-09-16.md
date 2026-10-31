# Topos 422 Proxy 默认档位策略验证（P2-02 / P2-03 / T-02）

日期：2026-09-16 · 分支 fix-ui · macOS x86_64（Intel 降级机，D-01）
证据基线：`docs/p0_baseline_report_macos_2026-09-16.md`、
`docs/topos_422_family_benchmark_2026-09-16.md`（T-01 复验）

## 1. 实证档位表（decode 路径，benchmark_runner 口径）

| 素材类别 | 分辨率 | steady p50 (fps) | steady p95 | seek p95 | 默认档位判定 |
|---|---|---:|---:|---:|---|
| Topos 422 native | 4K | 75.6 | 16.6ms | 16.3ms | **不套代理**（>25fps 实时线 3×） |
| Topos 422 native | 2K | 268-478 | ≤5.6ms | ≤4.6ms | 不套代理 |
| Topos proxy | 4K | 212 | 7.5ms | 7.4ms | 目标代理档 |
| Topos proxy | 2K | 740 | 1.5ms | 1.5ms | 目标代理档 |
| Topos 4444（alpha） | 4K | 40.9 | 26.7ms | 26.3ms | 不套代理（>25fps；4× alpha 平面开销） |
| H.264 | 2K | 447.6 | 3.4ms | 67.3ms | 不套代理 |
| H.265 10-bit | 4K | 44.5 | 27.7ms | **519ms** | **代理**（长 GOP seek 惩罚） |
| ProRes 422 HQ | 4K | 32.1 | 38.2ms | 37.4ms | **代理**（E-06，软解不过实时线） |
| ProRes 422 LT | 4K | 41.0 | 28.9ms | 30.9ms | 代理 |
| DNxHR HQX | 4K | 38.8 | 31.7ms | 34.8ms | 代理 |

## 2. 策略结论（已实现，`proxy_policy.py`）

1. **Topos 家族全分辨率豁免代理**（`topos_edit_codec_no_proxy_needed`）：
   native 解码全档位 ≥ 实时线 3×，代理无收益、纯耗磁盘与生成时间。
   `all` 模式同样豁免——Topos 本身就是优化中间片编码（D-06）。
2. **>1080p 生成保留**（P1-14 既有阈值），原因码按 codec 风险分级：
   `long_gop_above_threshold`（h264/h265/hevc/av1/vvc）、
   `heavy_edit_above_threshold`（prores/dnxhr/…）、`above_native_threshold`。
   原因码即 T-04/P2-04 的"按风险排序"输入：长 GOP seek 收益最大，优先。
3. **未知尺寸显式化**：`dimensions_unknown`（修复了旧实现把未知尺寸当
   "低于阈值"静默跳过，导致未探测素材的自动代理路径失效的 BUG）。
4. **建议的产品调优（未改默认，待确认）**：阈值放宽到 2560×1440——
   2K 家族实测全部 ≥142fps（H.264 447fps），代理收益不存在；
   需同步 P1-14 项目设置语义与用户文档。

## 3. 后台 QoS（P2-03，已接线）

`DiskProxyService.pause_all_generation()/resume_all_generation()`（既有
能力，此前零调用方）现由 `TimelinePanel._on_controller_state_changed`
驱动：PLAYING → 挂起全部生成；PAUSED/STOPPED → 恢复。幂等、best-effort，
失败不影响播放。与 P2-06 的"导入不抢满机器"共用该机制即可（后续把
`generate_proxy` 入队改为带优先级）。

## 4. 验收对照

- P2-02"根据分辨率、codec、bit depth…选择；可解释、可覆盖"：
  分辨率+codec 已进策略；可解释 = 原因码（policy_off / topos_edit_codec…
  / long_gop…）；可覆盖 = 项目级 `ProxyPolicySettings`（off/auto/all）
  + 手动 Generate Proxy 菜单（绕过策略）。bit depth 待素材 metadata
  暴露 pixel_format 位深后加入（当前 metadata 只有 codec 名）。
- P2-03"播放时暂停或降权；可暂停、恢复、取消、限并发"：挂起/恢复已
  接线；取消（cancel_generation/cancel_all_generations）与并发上限
  （ProxyManager 队列）为既有能力。
- T-02"自动选择有证据；用户可一键覆写"：本表即证据；覆写 = 菜单强制
  生成 / 策略模式切换。
