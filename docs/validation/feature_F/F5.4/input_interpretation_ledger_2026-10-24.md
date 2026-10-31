# F5.4 · 常见输入解释与通用图像序列导入 — 验证台账

- 日期：2026-10-24；计划条目：§F5.4
- 提交：片1 `d8faed366`（序列扫描模型）＋ 片2（VFR 生产绑定/组合核验）
- 测试：test_f54_generic_sequence.py（6）+ test_f54_vfr_alignment.py（7）＋回归（frame_mapper/scene_cut 映射 100）全绿

## 1. 交付

| 组件 | 内容 |
|---|---|
| 通用序列导入模型（片1） | `generic_image_sequence.py`：PNG/TIFF 子集 `<prefix><frame><suffix>` 聚合=单资产候选 + 缺帧诊断（holes 明示 1007 一类空洞）+ 孤立单帧不合流（显式选择）+ 文件集合指纹（移盘/重开一致） |
| VFR PTS 索引构建 | `extract_pts_index_seconds`（PyAV 顺序 demux 逐包 PTS×time_base；失败回退 CFR 不阻断导入）+ `looks_vfr`（相邻差 ±25% 判定） |
| VFR 生产绑定 | 探测端：`probe_media_metadata` 视频分支——VFR 才写 `pts_index`+`is_vfr`（CFR 不写零开销）；插入端：`insert_clip_at_playhead`/`InsertClipCommand.create`/`timeline_view` 调用点全链透传 → `ClipInstance._source_pts_index` → `timeline_to_source_frame` 走 timeline_frame_mapper VFR 分支（既有精确 PTS 求帧） |
| 输入解释组合核验 | VideoMetadata 携带 field_order/SAR/rotation（0/90/180/270 校验、SAR 非正拒绝、is_interlaced）；导出 passthrough 守卫对 rotation≠0/隔行拒绝直通（退化完整渲染不静默出错片）——同条件测试锁定 |

## 2. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 手机 VFR 长片音画不漂移 | `test_vfr_detected_and_cfr_not`（合成 ±30% 抖动 PTS 长片：VFR 判定/CFR 不误判）+ `test_probe_writes_pts_metadata_for_vfr`（导入 metadata 只对 VFR 携带索引）+ `test_frame_mapping_and_audio_clock_agree`（视频帧 PTS 秒 == 音频解码时钟秒，时间线帧 0/10/23/47 半帧容差内对齐——两侧共享同一索引） |
| 90°片/非方像素/隔行片方向/几何/时刻一致 | metadata 组合核验（field_order/SAR/rotation 解析+校验）+ 导出 passthrough 守卫拒绝测试；完整 Source→Edit→代理→导出几何链对拍见后续片 2 |
| 未支持组合可拒绝/转码 | passthrough 守卫拒绝 rotation/隔行（退化完整渲染）；拒绝而非静默出错片 |
| shot_1001 序列一个资产、缺 1007 有诊断 | 片1 测试（单资产候选/缺帧诊断/指纹） |
| trim/重开/移盘帧号与 alpha 一致 | 序列指纹（帧号+size+mtime 移盘稳定）；clip PTS 索引序列化往返既有（timeline_v2 1440/1554） |
| 不强制把连号照片合为序列 | 孤立单帧不合流测试（min_sequence_frames 门槛） |

## 3. 登记的后续片（不阻塞验收）

1. 序列资产→时间线插入链（image_sequence clip 的帧映射/alpha/trim 全链，
   模型与诊断已就绪，消费端接线）。
2. 90°/隔行/非方像素的 Source→Edit→代理→导出**端到端几何对拍**
   （metadata 与守卫已核验；合成 rotated/interlaced 素材的全链测试）。
3. EXR/DPX 序列（位深/alpha/AOV 按需求验证后开）。
4. reverse 素材策略声明（速度反放既有；源级 reverse 素材导入未见需求）。
5. 媒体池 UI：目录导入对话框的序列候选选择界面（classify_directory 已就绪）。

## 4. 诚实边界

- 音画不漂移验收在模型/索引层对齐（共享 PTS 秒表 + 既有 mapper/解码
  时钟）；带真实音频流的合成 VFR 长片全链听感验证登记后续片。
- 用户 Transform 与 rotation/SAR 的组合顺序：rotation/SAR 在源解释层
  （metadata+守卫），用户 Transform 在 clip 层——层次分离即组合语义；
  逐参数交互对拍随后续片 2 端到端覆盖。
