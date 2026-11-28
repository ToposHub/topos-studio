# Topos 422 Proxy 与 Apple ProRes 422 Proxy 画质对比

Topos 422 Proxy 在细彩纹样上确实更软。用户指定的北海庭院、彩绘屋檐和琉璃门三个镜头中，Topos 默认比 Apple ProRes 低 **2.42–4.15 分 4K VMAF**。按每个镜头的实际视频载荷匹配码率后，仍低 **1.84–3.74 分**。在这个码率区间，隔离实验仅移除 AC 量化死区即可使三个镜头回到 Apple ProRes 的 VMAF 水平，同时会扩大块边界误差。

## 用户指定的三个镜头

三张截图都匹配到《北海公园4K素材2》的 DNxHR HQX 母版。截图用于定位画面，评分和裁切取自源视频及两套编码的解码平面。帧号从 0 开始。前两列展示定位帧与参与评分的连续窗口。

| 镜头 | 定位时间与帧号 | 连续窗口 | 2× 同坐标对照 |
|---|---|---|---|
| 庭院中央的树枝、灰瓦、彩绘和墙面 | 12.04 秒，第 301 帧 | 260–323 | [庭院对照图](../../artifacts/topos_proxy_quality_20261128/courtyard_details.png) |
| 青绿琉璃瓦与多色斗拱 | 58.96 秒，第 1474 帧 | 1446–1503 | [屋檐对照图](../../artifacts/topos_proxy_quality_20261128/eaves_details.png) |
| 琉璃门的金绿花纹、红门和细窗格 | 64.16 秒，第 1604 帧 | 1574–1631 | [琉璃门对照图](../../artifacts/topos_proxy_quality_20261128/gate_details.png) |

每种编码在相同的 180 帧上评分。表中每格为 **实际视频 Mbps / 全画幅 4K VMAF**。同镜头码率使用该窗口 Apple 输出的平均视频载荷作为 Topos 目标，经典与死区为零的实验使用相同目标。色度与纹理评分使用图中标注的固定区域。

| 镜头 | Apple ProRes | Topos 默认 | Topos 匹配镜头码率，AC 死区 Q/4 | Topos 匹配镜头码率，AC 死区 0 |
|---|---:|---:|---:|---:|
| 庭院 | 139.97 / 99.48 | 133.47 / 95.33 | 138.48 / 95.74 | 139.18 / 99.50 |
| 彩绘屋檐 | 142.07 / 99.89 | 132.05 / 97.10 | 141.72 / 97.83 | 141.83 / 99.88 |
| 琉璃门 | 145.60 / 99.94 | 133.86 / 97.52 | 144.63 / 98.10 | 145.25 / 99.95 |

三个区域的亮度中频系数幅度，用解码画面与源画面的系数内积除以源画面的能量计算。100% 表示幅度和方向都与源信号一致时的基准值。它用来衡量瓦纹、彩绘描边和窗格等结构的强弱，配合原图与误差评分阅读。

| 镜头 | Apple ProRes | Topos 默认 | Topos 同码率 Q/4 | Topos 同码率死区 0 |
|---|---:|---:|---:|---:|
| 庭院 | 92.3% | 81.5% | 82.2% | 96.7% |
| 彩绘屋檐 | 91.8% | 79.6% | 81.2% | 96.2% |
| 琉璃门 | 96.4% | 87.1% | 88.3% | 97.6% |

琉璃门区域的 Topos 默认 U/V PSNR 比 Apple 各低 0.48 和 0.52 dB。匹配码率后，色度回到约 0.04 和 0.01 dB 的差距，亮度中频幅度仍相差约 8 个百分点。庭院和屋檐也出现同样的中频衰减。三个镜头的默认 Topos 区域 Y-PSNR 都略高于 Apple，说明均方误差可以在边纹变软时依然较低。参见 [庭院逐帧数据](../../artifacts/topos_proxy_quality_20261128/courtyard_details.json)、[屋檐逐帧数据](../../artifacts/topos_proxy_quality_20261128/eaves_details.json)和[琉璃门逐帧数据](../../artifacts/topos_proxy_quality_20261128/gate_details.json)。

## 两条母版全长结果

| 母版 | 编码 | 视频 Mbps | Y / U / V PSNR，dB | 4K VMAF | SSIM |
|---|---|---:|---:|---:|---:|
| 北海公园，1730 帧，25 fps | Apple ProRes Proxy | 146.18 | 40.11 / 46.42 / 47.37 | 99.68 | 0.98559 |
| 北海公园 | Topos 422 Proxy 默认 | 133.19 | 40.35 / 46.18 / 47.23 | 98.07 | 0.98478 |
| 北海公园 | Topos 匹配 Apple 全片码率 | 145.61 | 40.96 / 46.63 / 47.65 | 98.51 | 0.98636 |
| 圆明园，709 帧，30000/1001 fps | Apple ProRes Proxy | 181.03 | 41.58 / 50.07 / 49.41 | 98.21 | 0.98053 |
| 圆明园 | Topos 422 Proxy 默认 | 159.72 | 41.36 / 49.28 / 48.43 | 94.19 | 0.97783 |
| 圆明园 | Topos 匹配 Apple 全片码率 | 180.34 | 42.32 / 49.75 / 48.94 | 95.18 | 0.98130 |

码率按实际视频包字节数计算。PSNR 基于两段全长的 2,439 帧，先聚合每个平面的平方误差，再按 10-bit 满刻度 1023 换算。VMAF 使用 `vmaf_4k_v0.6.1`，和 SSIM 一起在每条片的五个分散窗口上抽测，每窗 16 个连续帧。FFmpeg `prores_ks` 在相同抽测窗口的 VMAF 为 93.93 和 89.59。Apple ProRes 是主基准，软件实现之间的结果差异足以改变结论。 [FFmpeg ProRes 编码器文档](https://www.ffmpeg.org/ffmpeg-codecs.html)

## 根因与反证

1. **AC 量化死区压低了纹样的亮度对比。** 默认 AC 死区是 Q/4。首次非零量化阈值约为 0.75Q，超过阈值后的系数也会受量化偏移影响。现有注释说明，Q/4 的原始扫频工作范围是 1.4–3.6 bpp，本次 UHD Proxy 默认目标为 0.64483 bpp。实测在两条片的各 200 帧实验中，匹配码率后将死区调为 0，VMAF 从 98.56 提升到 99.78、从 95.23 提升到 98.39。圆明园实验的 Y-PSNR 同时下降 0.44 dB，8 像素网格上的额外亮度残差台阶从 2.95 升到 4.02 码值。Q/8 在圆明园取得 VMAF 96.81、Y-PSNR 42.48 dB，网格额外台阶 3.31 码值。实验仅改动隔离库的编码量化，输出由当前正式代码解码。 [量化实现](../../native/topos_codec/src/transform/quant.c#L288)
2. **Proxy 默认码率低了约 9–12%。** 档位设定为 0.64483 bpp，UHD 每帧目标 668,560 字节。实测两条片分别比 Apple 少 8.89% 和 11.77% 的视频载荷。对齐实际全片码率到 0.4% 内后，感知差距仍有 1.18 和 3.03 分。Net Proxy 独立档位是 0.29108 bpp，圆明园抽测实出 72.35 Mbps、VMAF 84.07。 [档位参数](../../src/shared/codec/topos_profiles.py#L135) · [目标字节换算](../../src/shared/export/topos_encoder.py#L2977)
3. **码控对少数帧分配不足。** 全片默认最差亮度差出现在北海第 1081 帧和圆明园第 447 帧，分别低 2.83 和 2.94 dB。北海即使匹配全片平均码率，该帧仍只拿到 454,284 字节，Apple 同帧有 740,704 字节。只重编码这一帧，以 Apple 同帧载荷为上限做当前帧 QP 搜索，QP 从 84 降到 81，载荷为 684,491 字节，Y-PSNR 从 39.05 到 41.90 dB。圆明园同样的复测把匹配全片版的 43.66 提到 44.65 dB。当前反馈律主要用上一帧包大小更新下一帧 QP。每帧硬上限也是不同的取舍，圆明园抽样直接套用 Apple 逐帧字节上限，实际只达到 Apple 平均码率的约 92%。 [码控反馈](../../src/shared/export/topos_rate_control.py#L150) · [单帧复测和包头统计](../../artifacts/topos_proxy_quality_20261128/rate_probe.json)

当前 UHD SDR Proxy 使用平坦矩阵 QM0，并开启去块。匹配码率切回 QM4 后两条片的抽测 VMAF 分别降 0.32、0.86 分。北海启用现有细化 QP 表仅差 0.004 分，启用切片 AQ 下降 0.23 分。把圆明园色度 QP 降两级可提高 U/V PSNR 约 0.89 / 0.86 dB，代价是 Y-PSNR 下降 0.47 dB，VMAF 下降 0.54 分。禁用去块会增加两片的 8 像素亮度网格台阶。Topos 默认抽查 123 帧，102 个切片的 QP 偏移始终为零。同期 Apple 包头各帧只有 1–3 种 QScale，量化级数量本身解释不了这次差距。 [Topos 矩阵和特性选择](../../src/shared/export/topos_encoder.py#L1192) · [ProRes 切片头解析](https://ffmpeg.org/doxygen/trunk/proresdec2_8c_source.html)

## 测试口径与复现

- 两条现成源都为 DNxHR HQX、3840×2160、10-bit 4:2:2、BT.709 limited。保持母版帧率和原生尺寸，无音轨、调色和缩放。Apple 使用本机 VideoToolbox Proxy，允许软件编码且本机的强制硬件冒烟失败。每条母版前 24 帧核对了 FFmpeg 和 PyAV 的原始平面哈希，以及 Apple 所需 P210 打包往返哈希。
- Topos 调用交付所用的 `ToposVideoEncoder`，以相同 10-bit YUV 平面直入。实际 MOV 码流由 Topos 原生解码，评分步骤检查样本数、可见尺寸、像素格式、位深及解码 conceal 状态。时序由帧 PTS 与对应源索引核对。
- 主测试动态库从仓库 `16093534` 的源码独立编译，SHA-256 为 `9842a54450362248da88600076e7bc2f9f43e768865b0e5ad49ad94eb1bf8458`。仓库默认旧库与新库的圆明园 200 帧同配置抽测在每帧 QP、视频包长和误差指标上完全一致。测试覆盖这两条 4K 实拍母版与交付编码器，素材库的“生成代理”另走一条实现，交互预览也有独立缩放路径。 [素材库代理生成](../../src/shared/media/proxy_generator.py#L1031)
- [编码和逐帧评分脚本](../../artifacts/topos_proxy_quality_20261128/measure.py)、[VMAF 和 SSIM 脚本](../../artifacts/topos_proxy_quality_20261128/perceptual.py)、[细节区域脚本](../../artifacts/topos_proxy_quality_20261128/scene_detail.py)、[死区 0 实验补丁](../../artifacts/topos_proxy_quality_20261128/deadzone-zero.patch)及[死区 Q/8 实验补丁](../../artifacts/topos_proxy_quality_20261128/deadzone-eighth.patch)随报告归档。本次逐帧 JSON、裁切图和复现脚本统一归档在 `artifacts/topos_proxy_quality_20261128`。前期实验使用隔离的 native 库。

## UHD Proxy 优化复测

普通 3840×2160、Rec.709 limited、无 Alpha 的 RANS2 帧内 MOV Proxy 导出，默认使用 AC 死区 Q/8 和 0.70 bpp。对单帧载荷偏差的码控反馈增益从 8 调到 5，以减少一帧多给、下一帧少给的现象。自定义码率依然优先。固定 QP 和硬字节预算沿用各自原有配置。旧动态库通过能力位检测继续使用经典编码路径。

| 母版 | 旧版 Mbps / VMAF | 优化版 Mbps / VMAF | 优化版 Y / U / V PSNR，dB | 较 Apple 低 2 dB 以上的亮度帧 |
|---|---:|---:|---:|---:|
| 北海公园 | 133.19 / 98.07 | 144.01 / 99.22 | 41.11 / 46.78 / 47.77 | 2 → 0 |
| 圆明园 | 159.72 / 94.19 | 172.98 / 96.55 | 42.21 / 49.66 / 48.82 | 12 → 0 |

指定镜头的 VMAF 为庭院 98.16、彩绘屋檐 99.64、琉璃门 99.29，较旧默认分别增加 2.83、2.55 和 1.77 分。两条全片的新码率分别低于 Apple 约 1.49% 和 4.44%。与 Apple 同档的色度差在圆明园仍可测出，优化版 U/V PSNR 低 0.40 / 0.60 dB。

优化版在两条源共 2,439 帧上重新编码并逐帧解码。北海和圆明园相邻帧 QP 变化至少两级的次数分别由 85 降到 9、由 317 降到 1。新旧动态库对抽查的旧版与新版码流重建出相同的 Y/U/V 码值。 [北海优化版逐帧分数](../../artifacts/topos_proxy_quality_20261128/beihai_topos_optimized.score.json) · [圆明园优化版逐帧分数](../../artifacts/topos_proxy_quality_20261128/yuanmingyuan_topos_optimized.score.json) · [三个指定镜头的优化版 VMAF](../../artifacts/topos_proxy_quality_20261128/beihai_topos_optimized_260-64_1446-58_1574-58.vmaf.json)

## 素材库与时间线 4K 播放验证

素材库使用 800×540 窗口打开北海优化版和 Apple ProRes 对照，默认送显帧都是 3840×2160。手动切换半分辨率、四分之一、全分辨率，实测依次是 1920×1080、960×540、3840×2160，暂停状态下可操作进度条定位至第 1604 帧。抽查北海指定三帧，Topos 全分辨率播放的 Y/U/V 码值与独立原生解码一致。 [素材库记录](../../artifacts/topos_proxy_quality_20261128/library_full_playback.json) · [琉璃门播放截图](../../artifacts/topos_proxy_quality_20261128/library_full_resolution_player.png)

时间线使用 960×556 左右的监看窗口打开同一 4K 文件。4K 时间线从全分辨率降到半档、四分之一再回全档，实际送显尺寸为 3840×2160、1920×1080、960×540、3840×2160。1080p 时间线仍读取和解码 3840×2160 原片，按 1920×1080 合成。本机验证出图使用 OpenGL，Metal 至 OpenGL 的 IOSurface 桥在本机转入 OpenGL 回退。 [监看窗口实测日志](../../artifacts/topos_proxy_quality_20261128/program_preview_gl.log) · [源帧验证脚本](../../artifacts/topos_proxy_quality_20261128/verify_program_preview_decoding.py)

## 低分辨率预览的块状感

普通 Topos 的旧降档预览逐 8×8 块舍弃高频系数，再按目标网格取单点。素材库和时间线的新预览先完整解码，再在原位深的各平面做面积缩小。复测北海的庭院、彩绘屋檐及琉璃门三帧，1/2 和 1/4 档的 Y/U/V 与完整解码后的面积平均误差不超过 0.5 个码值。监视器仍按档位呈现 1920×1080 或 960×540，解码信息显示 3840×2160。

庭院第 301 帧在 1/4 档，两个色度平面的编码块边界梯度比全图平均相邻梯度高约 21% 和 23%，修复后降为约 5% 和 5%。Apple ProRes 422 Proxy 在素材库相同档位约为 2% 和 2%。 [同帧裁切对照](../../artifacts/topos_proxy_quality_20261128/quarter_preview_before_after.png)
