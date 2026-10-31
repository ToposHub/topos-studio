# F5.3 · 相机 RAW 与媒体支持矩阵 — 验证台账

- 日期：2026-10-24；计划条目：§F5.3
- 测试：tests/media/test_f53_media_matrix.py（16）全绿

## 1. 交付

| 组件 | 内容 |
|---|---|
| 支持矩阵 | `shared/media/support_matrix.py`：10 格式家族 × 六维能力（container/decode/raw_controls/proxy_seek/export + evidence/notes）；AVAILABLE 项全部绑定真实原片或代码锚点证据 |
| 诊断路径 | `diagnose_media_path`：BRAW/R3D（真实原片在库但无 SDK）→ `sdk_missing` + 缺失项明细 + **转码建议**（ProRes 422 HQ/DNxHR HQX 母版路径），不静默失败；未知扩展名走通用尝试声明 |
| 单列声明 | 照片 RAW（LibRaw/rawpy，源级 WB/曝光/去拜耳经 raw_params）与 Topos RAW（.traw，factory topos raw 分支）**分别单列**，不与相机视频格式混标 |
| 矩阵载荷 | `matrix_to_payload` 供 capability_registry/UI 消费（AVAILABLE 必须有证据字符串——测试锁定） |

## 2. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 每个承诺格式有真实原片 | 10 个真实文件抽样矩阵测试（h264×2/h265×2/prores×2 含 Alpha/dnxhr×2 含 12bit/topos×2），每文件真实打开+解码一帧（FFmpeg 家族直开；Topos 私有编码经 ToposMediaSource 按其 read_next_frame 契约） |
| 文件扩展名已识别不算验收完成 | AVAILABLE 判定绑定 decode 动作（非扩展名识别）；矩阵测试断言帧数据非空 |
| 源级白平衡/曝光/去拜耳对预览和成片产生预期结果 | 照片 RAW：rawpy 路径（image_engine.py Auto WB 锚点）+ raw_params 全链（TRAW 批 2 既有）；Topos RAW：raw 分支既有——能力声明 available + 证据锚点（逐参数预览/成片对拍属既有 raw 链测试族） |
| SDK 缺失给诊断与转码建议 | `test_braw_real_file_diagnosed_with_transcode_advice`/`test_r3d_real_file_diagnosed`（真实 .braw/.R3D 在库，诊断含 Blackmagic/RED 缺失项 + 转码建议文案） |
| 不承诺一口气全部支持 | BRAW/R3D/CinemaDNG 全维 unavailable/diagnostic 且 notes 声明原因（许可证/分发/ABI 未落实；CinemaDNG 序列归 F5.4）；`test_unavailable_formats_not_claimed` 锁定不冒称 |

## 3. 登记的后续片（不阻塞验收）

1. BRAW/R3D SDK 引入（先核许可证/分发/平台 ABI 与取消/释放——计划原文
   门槛；真实原片已在库可作验收素材）。
2. CinemaDNG 序列（F5.4 图像序列链联动）。
3. `diagnose_media_path` 接入媒体导入入口（导入失败路径给诊断而非
   通用错误——UI 接线一片）。
4. 矩阵入 capability_registry 条目（payload 已就绪）。

## 4. 诚实边界

- 源级 RAW 控制的"预览与成片一致"沿用既有 raw 链对拍（TRAW 批 2 +
  ColorStackData.raw_params 消费链）；本批新增的是**矩阵声明+真实原片
  验收+诊断路径**，不重复已验证的 raw 数值链。
- 矩阵的 proxy_seek/export 维度为家族级声明（具体格式逐位对拍属
  F5.1/导出测试族既有覆盖）。
