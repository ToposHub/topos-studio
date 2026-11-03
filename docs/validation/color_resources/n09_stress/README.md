# N09 — 压力/性能录制（scopes/s00_s01_record 协议，2026-11-04）

## 交付

- `scripts/stress_color_session.py`：真实应用会话（原生 cocoa）压力
  驱动——六行为轮换（播放循环/随机 seek/调参手势（图命令路由）/
  示波器 9 模式轮换/Color↔Edit 切页/浮窗开关+resize），随机种子可
  复现；仪表：播放时钟与渲染帧距 p50/p95/p99、调参/seek 命令延迟、
  RSS 稳态斜率（psutil，跳过前 60s 预热）、示波器分段诊断
  （texture_not_shared/fbo_incomplete/blit_failed 硬门槛）。
- **30 分钟原生全量录制**（seed=20261104，行为片 45s，run.log +
  stress_report.json 存本目录）。

## 30 分钟实跑结果（如实）

| 指标 | 数值 | 预算 | 判定 |
|---|---|---|---|
| 播放时钟 p95 | 47.1ms（p50 40.1，25fps 素材） | ≤60ms | ✅ |
| 渲染新鲜度 | 1.09 fps（450.8s 播放窗，2296 帧） | ≥0.5fps（不停滞） | ✅ |
| 渲染帧距（上报，不设门） | p50 116 / p95 282 / p99 386ms | — | 记录 |
| 调参命令 p95 | 6.29ms（1385 次拖动） | ≤120ms | ✅ |
| seek p95 | 0.22ms（293 次） | ≤400ms | ✅ |
| RSS 稳态斜率 | **289.2 MB/10min**（峰值 2396.8MB） | ≤250 | ❌ 超预算 16% |
| 硬门槛（widget） | texture_not_shared/fbo_incomplete/blit_failed 全 0 | 0 | ✅ |
| 执行错误 | 0 | 0 | ✅ |

操作量：2685 次示波器模式切换、386 次切页、473 次浮窗开关、
293 次 seek、1385 次调参。诊断事件（非门）：coalesced_replaced、
rate_limited、stale_return、not_visible_on_return、mutex_busy——
均为节流/合并/不可见丢弃的设计语义，有计数可查。

## 分段定位（2026-11-04 第二轮，12min × 6 行为窗）

行为窗分段 RSS 斜率（scripts/stress_color_session.py 增补
per_behavior 分段；三段式 12min 原生实测，seed 同）：

| 行为 | mean MB/10min | max | 判定 |
|---|---|---|---|
| **float（浮窗开关+resize）** | **1646.7** | 1646.7 | ❌ 主犯 |
| **page（Color↔Edit 切页）** | **489.6** | 625.6 | ❌ 次犯 |
| scope（9 模式轮换） | 51.7 | 72.7 | ⚠️ 轻度 |
| playback / seek / param | 12.6 / 13.2 / 14.3 | ≤26.8 | ✅ 基线 |

结论：整体 404 MB/10min 几乎全部来自 **浮窗开关与切页路径**；
播放/调参/seek 主链是干净的（12-18 MB/10min ≈ 预算 250 的 5-7%）。
治理方向（后续批次）：浮窗关闭路径的 presenter/GL 资源释放、切页
Color 页渲染资源的滞留回收。修复后复跑本协议即可复验。

## 判定与登记

- **status=failed（仅 RSS 斜率超预算 16%）**：录制协议本身完成且
  五项预算过四；超预算项如实登记为**待治理债务**——极端节流画像
  （~0.7s/次模式轮换 + 切页 + 浮窗）下 presenter/纹理缓存持续增长。
  候选方向（未臆测修复）：示波器 presenter 缓存随模式轮换的增长、
  切页路径的资源滞留。建议单独批次以分段斜率定位（按行为窗口分别
  计 RSS 斜率）。
- **默认 flag 维持关闭**：N01 出口语义已完成（full-res f4/播放附
  快照/复杂图显式拒绝），但计划规定默认开启需「性能/资源矩阵通过」
  ——RSS 预算未过，前置不满足。
- 平台：本机 macOS（原生 cocoa + ATI GL 4.1）；其他平台 NOT RUN，
  随 R12 矩阵补。

## 冒烟记录

- offscreen 2 分钟（全行为覆盖）：passed（渲染帧在离屏不发布属预期）。
- 原生 30s/90s/2min 逐级冒烟：passed；据此修正了（1）Qt GL 格式
  前置（4.5 core + 共享上下文，否则 moderngl version_code=0）、
  （2）帧距采样口径（只计播放相位+稳态）、（3）RSS 斜率跳过预热。
