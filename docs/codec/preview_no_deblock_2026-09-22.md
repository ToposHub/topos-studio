# 预览关滤波开关（TC_DECODE_FLAG_NO_DEBLOCK / video_preview_no_deblock，2026-09-22）

承接 `docs/topos_prores_quality_gain_2026-09-22.md`（P6 去块 v2.1）与
`docs/codec/topos_speed_matrix_2k_4k_2026-09-22.md`（滤波解码开销 1.19–2.02×）。

## 0. 决策

**默认保持滤波**（含默认的「流畅」档与 1/2 档），把"关滤波"做成**显式偏好开关**
（`video_preview_no_deblock`，默认关）。理由：默认档就是"流畅"，而 8×8 块界台阶
恰恰是那里最显眼的伪影（v2.1 的缩放交付滤波正是为它加的）；关掉它换来的
预览提速是 1.11–1.37×（端到端预览帧口径），而**卡顿的主要杠杆是档位**
（1/2→1/4 是 3–4×），不是滤波。弱机需要那 1.2–1.4× 时，勾一下这个开关即可。

## 1. 实现

位流侧**不动**（去块仍由帧头 `flags` bit2 承载）；新增的是**解码请求位**：

| 层 | 改动 |
| --- | --- |
| C 头 | `TC_DECODE_FLAG_NO_DEBLOCK (1u<<1)` + `TC_CODEC_CAP_DECODE_NO_DEBLOCK (1u<<8)`（`include/topos_codec.h`） |
| C 内核 | 请求校验接受 bit1；`tc_decode_plan.no_deblock` → `dec_frame_req.no_deblock` → `dec_frame_finalize` 门控；`tc_v7b_frame_decode_ex(..., flags)` 覆盖 V7-B FULL 路径；缩放交付同一门控 |
| 绑定 | 常量 + 校验白名单 + `caps.decode_no_deblock` 访问器 |
| 媒体源 | `ToposMediaSource(no_deblock=…)`：先查能力位（旧库对未定义位**干净拒绝**，故 **fail-closed 到保持滤波**并留一行 INFO 日志），再置位到请求 |
| 工厂 | `MediaSourceFactory.create(..., no_deblock=)`，显式后端白名单 `_NO_DEBLOCK_BACKENDS={'topos'}`（不做签名反射） |
| 解码管理器 | `_DecodeSourceSpec.no_deblock` 进 `_SourceKey`（切换即换 source，不误用旧 request）+ `set_no_deblock()` 整体失效 |
| 渲染器 | `TimelinePreviewRenderer.set_no_deblock()` / worker 同名 setter：透传 + 预览缓存失效 + 代际重置 + 重渲当前帧（与 `set_preview_quality` 同套路） |
| 偏好 | 配置键 `video_preview_no_deblock`（默认 False）；面板「视频管线」组勾选框；10 语言文案；偏好对话框关闭后即时生效（无需重启） |

**作用域**：只预览/播放。导出与交付路径不置该位——滤波在那里保持生效。

## 2. 实测（预览口径）

口径：`ToposMediaSource(target_size=…)` → `read_frame(i)`（含容器读取与平面池，
即**预览真实每帧成本**）；画质对照母版 reformat 到**同一目标尺寸**；2K 源 → 960×540、
4K 源 → 1920×1080（各等于"时间线 1/2 预览"）。时间取稠密帧（0..47）五轮中位数。

| 源（预览目标） | 滤波 ms/帧 | 关滤波 ms/帧 | 提速 | block_h 前→后 | block_v 前→后 | Y PSNR |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2K proxy → 960×540 | 4.10 | 2.71 | **1.51×**（三轮 1.28–1.51×） | −0.82 → **+3.49** | 样本不足 | 34.84 → 34.82 |
| 2K standard → 960×540 | 5.34 | 4.97 | **1.07×**（1.07–1.22×） | −0.32 → **+0.95** | 样本不足 | 35.05 → 35.05 |
| 2K HQ → 960×540 | 5.98 | 5.22 | **1.15×**（1.11–1.19×） | +0.06 → **+0.80** | 样本不足 | 34.94 → 34.94 |
| 4K proxy → 1920×1080 | 12.22 | 9.28 | **1.32×**（1.24–1.32×） | −0.17 → **+1.01** | −0.30 → **+1.02** | 38.93 → 38.93 |
| 4K standard → 1920×1080 | 17.74 | 16.70 | **1.06×**（1.05–1.20×） | +0.06 → **+0.47** | +0.11 → **+0.58** | 39.02 → 39.02 |
| 4K HQ → 1920×1080 | 19.13 | 17.47 | **1.10×**（1.08–1.15×） | +0.12 → **+0.38** | +0.13 → **+0.41** | 38.85 → 38.85 |

> 表内是最后一轮（5 轮 × 48 帧中位数）；括号是同一单元格三轮独立运行的摆动范围。
> 本机噪声 ±12%（同二进制同码流回读摆动），**单次 1.0× 附近的读数不可当结论**
> （曾出现 2K standard 0.91× 的反向噪声）。block_* / PSNR 两列是确定性的
> （指标在同样的 5 帧上算，逐轮逐位一致）。

三行读法：

1. **提速 1.11–1.37×（预览整帧口径）**；纯解码口径更接近 1.6–2.0×
   （速度矩阵 on/off）——预览帧还背着容器读取与平面池开销，所以滤波占比被摊薄；
   低档（proxy，块界台阶最多）提速最大，高档（HQ）最小。
2. **代价是块界台阶回到未滤波水平**：4K 预览 +0.4…+1.0 LSB、2K 预览最高 **+3.49 LSB**
   （proxy 档，10-bit 域），即"预览小色块"重现；`Y PSNR` 几乎不动（±0.02 dB）——
   这是**观感交换**而不是保真度损失。
3. 与档位杠杆对比：`1/2 → 1/4` 是样本数 4×、解码 3–4×；滤波只有 1.2–1.4×
   ⇒ 卡顿优先降档，不够用再勾这个开关。

主观验收面板（母版 / 滤波 / 关滤波，三方同窗放大；裁片自动选**平坦区**
——块效应在那里最显眼，边缘密集处反而看不见）：

```bash
.venv/bin/python tools/bench_topos_preview_filter.py \
  --files tests/test_videos/2K_topos_422_Proxy.mov --target 960x540 \
  --reps 3 --time-count 24 --crops 2
# → bench_out/qgain/nodeblock_2K_topos_422_Proxy_f486_c*.png
```

证据：`bench_out/qgain/preview_nodeblock_ab_960x540.json`、
`bench_out/qgain/preview_nodeblock_ab_1920x1080.json`。

## 3. 测试

- **C**（`native/topos_codec/tests/unit/`）：`test_codec.c` 加 flag 契约（bit1 合法并
  映射到 `plan.no_deblock`、bit2 拒绝）与**内核等价不变式**——置位的解码输出逐位
  等于"未滤波输出 + 手工施加 `tc_deblock_plane`"（16 组 (qp, 台阶幅度) 组合全部
  成立；且至少一组确实落像素，避免恒等通过）；`test_abi.c` caps 精确集合。
  `ctest` **90/90**（debug）。
- **pytest**：绑定位/组合位/上位拒绝与能力位（`tests/native/test_topos_binding.py`）；
  **源级端到端**（`tests/media/test_topos_source.py::test_no_deblock_request_skips_output_filter`）
  用真实产品流（2K proxy）钉死"开关真的改变画面、且不带开关时与带滤波逐位一致"；
  管理器透传（`tests/edit/test_edit_gpu_decode_manager.py`）；渲染器 setter 幂等
  （`tests/edit/test_adaptive_playback_quality.py`）；偏好页 load/apply/restore 与
  默认值完整性（`tests/architecture/test_module_preferences.py`）；能力位与 manifest
  同步（`tests/media/test_capability_manifest.py`）。
- 回归：`tests/{media,native,architecture,edit}` 共 **11955 passed**；11 处失败中
  **2 处由本改动引起**（`_create_topos_source` 多一个参数 → 两处 kwargs 断言，已补
  `no_deblock: False`），其余 9 处逐个复验为既存或套内顺序敏感
  （benchmark manifest 漂移 1、Metal bridge 3、boundary guardrails 2、
  `test_content_localization` 1、音频路由/媒体池 3 处单独跑通过）。
  `tests/app` 341 passed。`tests/ui` 整包会**段错误**：定位到
  `test_mg_user_journey_regressions.py` + `test_motion_graphic_manual_controls.py`
  这个组合（崩在 `test_page_timing_panel_emits_global_and_page_backgrounds`，
  pytest-qt 事件处理里）。**父提交 95c720ce 上同样崩：5 轮 4 次**（两个文件单独
  跑分别 38/40 passed）⇒ 既存的 Qt 顺序敏感段错误，与本改动无关（本改动只扰动
  时序：带着改动 3 轮 2 次，统计上与基线同量级）。

## 4. 未做 / 后续

1. **1/3、1/4 档的滤波**（本批只做了"跳过"开关，没做"补上"）：目标域网格在
   d=3 时非整数（8/3）、d≥4 时滤波窗口重叠（相邻边界相隔 2 < 窗口 4），两者都
   不满足现有"窗口不相交 ⇒ 并行逐位等价"的证明前提。可选设计：目标域 **2 抽头
   无扩散**变体（只需 {p0,q0}，窗口 2 ≤ 间距）或**行缓冲 compute-then-write**。
   这同时消掉"自适应降档到 1/4 时静默丢滤波 + 块界周期变非整数"的观感跳变。
2. **CLI 图片默认是否开 DEBLOCK**：另一条零体积杠杆（`docs/image/toos_deblock_verify_2026-09-22.md`
   §2.4），代价是图片解码时间，仍待产品决策。
3. 自适应质量阶梯没有接"压力大时自动关滤波"：本批保留了用户显式开关这一条路，
   自动降级会引入"块感忽有忽无"，留待 1/3、1/4 可滤波后再评估。
