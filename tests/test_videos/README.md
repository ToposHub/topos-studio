# tests/test_videos — 真实媒体基准语料（不入库，清单已冻结）

本目录的媒体文件体积大（单文件最大 ~4 GB），**不进 git**（`.gitignore` 白名单
只放行 `*.json` 清单与本 README）。真实媒体回归的可检查口径是已提交的清单：

| 清单 | 内容 | 生成工具 |
| --- | --- | --- |
| `topos_prores_baseline_manifest.json` | 2K/4K × 六档 ProRes + TOPOS 基准集：487 帧 @25fps、pcm_s24le 音轨、逐档 bytes/帧数/色彩/音频校验与 `sha256_partial` 指纹、成对一致性 | `tools/generate_topos_baseline_corpus.py` |
| `benchmark_media_manifest.json` | 全目录 ffprobe 元数据 + 首尾 1MB 指纹 + B-01..B-06 场景映射 | `tools/freeze_benchmark_media_manifest.py` |
| `ai_test_asset_manifest.json` | AI 编辑测试资产 | 见文件内 generator 字段 |

## 重建方式（分发 = 本地重建，不做二进制分发）

```bash
# 需要先具备 ProRes 源（4K_prores4444.mov 母版 + Apple 原生 ProRes 集）
.venv/bin/python tools/generate_topos_baseline_corpus.py        # 全流程
.venv/bin/python tools/generate_topos_baseline_corpus.py --skip-prores  # 只重导 TOPOS 集
.venv/bin/python tools/freeze_benchmark_media_manifest.py       # 重冻 ffprobe 清单
```

重建后必须 `tools/freeze_benchmark_media_manifest.py --check` 无漂移、
`tools/verify_topos_quality_gates.py --require-media` 全过（C03 门禁）。

## 命名契约（2026-09-22 冻结，勿再改）

TOPOS 集：`{2K,4K}_topos_{422_LT,422_Proxy,422,422_HQ,4444,4444_XQ}.mov`。
历史小语料名（`2K_topos_proxy.mov` / `2K_topos.mov` 等）已退役；测试代码
一律用 `tests/real_media.py` 的 `require_video()/canonical_video()` 取素材，
缺失时默认 skip、`TOPOS_REAL_MEDIA_STRICT=1`（real-media 专用作业）直接 fail。
