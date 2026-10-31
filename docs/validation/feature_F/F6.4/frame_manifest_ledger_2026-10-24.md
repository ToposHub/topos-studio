# F6.4 · PNG/TIFF 等通用图像序列输出 — 验证台账

- 日期：2026-10-24；计划条目：§F6.4
- 测试：tests/shared/test_f64_frame_manifest.py（8）＋既有 exporter 套件 57 全绿

## 1. 现状核验

`ImageSequenceExporter` 既有：PNG/TIFF/EXR/DPX/JPEG 格式、色彩策略
（PNG→sRGB）、位深模式、文件名模式、uint8/16 归一——外部回导一致性
的基础设施已在（aud4 57 项测试）。**缺口 = 恢复语义**：跳过条件只看
"文件存在"，暂停/断电后的损坏帧、截断帧、异配置残留会被跳过（计划
明文"不只看文件名存在"）。

## 2. 交付

| 组件 | 内容 |
|---|---|
| 已验证帧 manifest | `VerifiedFrameManifest`：每帧成功导出记录 (路径,大小,头部 1MB SHA256)；`is_verified` = 文件存在**且**指纹匹配；save/load JSON（schema topos.frame_manifest/1）；缺失/损坏 manifest = 全部重导（fail-safe） |
| exporter 接入 | 成功导出即 record；跳过条件从"文件存在"升级为"存在且 manifest 指纹验证通过"——损坏/截断/异配置残留重导；旧行为可回退（manifest 为空时同旧） |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 暂停/取消/断电恢复不跳过坏文件 | `test_corrupted_file_not_verified`（篡改 1 字节失效）、`test_truncated_file_not_verified`（断电截断失效）、`test_resume_skips_only_verified_frames`（恢复会话：完好帧跳过、损坏帧重导替换） |
| 不混不同配置 | `test_different_config_residual_reexported`（旧配置残留按指纹失效重导） |
| 恢复依据已验证帧 manifest | record/verify/保存/加载往返 + 缺失/损坏 manifest 全重导（fail-safe） |
| 外部回导帧数/帧号一致 | 既有 exporter 套件（文件名模式/位深/归一 57 项）+ manifest 逐帧记录 |
| Topos 自有序列继续兼容 | .toos 序列路径零改动（F5.4 片1 扫描模型并列；本批只动通用 exporter） |

## 4. 登记的后续片（不阻塞验收）

1. DELIVER job UI 的"恢复导出"按钮接 manifest（核心已就绪）。
2. 外部合成软件（Nuke/AE）实机回导逐帧对拍（仓外环节；位深/alpha
   基础设施在，逐像素对拍登记实机后补）。
3. EXR/DPX 的 AOV/颜色标签扩展（F5.3 矩阵联动，按需求开）。
