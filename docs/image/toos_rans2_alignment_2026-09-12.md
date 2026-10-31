# 图片链路对齐视频优化集（.toos rans2 对齐）

日期：2026-09-12 · 前置：基准 `61a7782b` + 信封解封 `63e7b7e0` · 本批：对齐收口
依据实测：`tools/bench_toos_entropy_2026-09-12.json`（2K/4K 真实素材 ×4 熵链）

---

## 一、盘点：视频优化集中图片链路缺什么

| 视频侧优化 | 图片侧状态（对齐前） | 本批处置 |
|---|---|---|
| rans2 默认熵（V7-R2，ADR-C038） | `toos encode` CLI 默认 vlc；编辑器图片序列导出**静默降级** rans2→v2 | CLI 默认档翻转 + **删除序列导出降级** |
| AQ 色度逐带 qp 偏移（reserved[1]） | CLI vlc 路径开、rans2 路径没给 | rans/rans2 路径同开（AQ 与熵无关，视频编码器校验仅禁 intra/pair） |
| RDO 逐系数精修（reserved[2]） | — | **不可移植**（码率模型基于 V2 VLC 符号位，视频侧同约束）——显式 `--rdo on`×非 vlc 报错，不静默忽略 |
| 编码器内部优化（C5 bitwriter 批写、并行带编码、token 差分机） | 已共享（同 `tc_frame_encode`） | 无需动作 |
| GPU 解码融合（A2-4） | 图片解码 = 单帧 CPU `tc_image_decode` | 维持 CPU（GPU 融合是编辑器集成项，前置 = kernel 优化追平 CPU，见 `topos_v8_editor_integration_2026-09-12.md`） |

## 二、本批改动

1. **`ToposImageSequenceEncoder` 删除 rans2→v2 覆写**（topos_encoder.py）：
   信封解封 V1..V8 后，图片序列导出跟随视频熵档（默认 rans2）原生入 .toos。
   `timeline_export` 的 `topos_image_sequence` codec 直传配置，自动受益。
2. **`toos encode` CLI 默认熵 vlc → rans2**（image_cli.c）：
   - rans/rans2 路径 AQ 开启（与 vlc 路径同图片档策略）；
   - `--rdo on`×非 vlc 显式报错（跟踪显式旗标，默认值随熵档解释）；
   - 用法文本/注释同步（默认档、实测数字、旧读端拒收语义）。
3. **`ToposImageInfo.payload_version`** 暴露（Python 绑定）——容器/载荷
   版本诊断面，测试断言用。

## 三、验证

- **CLI 冒烟**（320×180 合成噪声图）：默认 rans2+AQ 157,462B vs
  vlc+AQ 179,929B（**−12.5%**）；verify --deep OK；解码字节数精确；
  XQ（GBR 444 12bit）rans2 258,267B + verify deep OK；
  `--entropy rans2 --rdo on` 正确拒绝。
- **pytest**：序列导出测试改写为 rans2 原生语义（`reserved[0]==8`、
  信封 `payload_version==(7,0)`、解码平面 3）；extract 测试钉死
  V7-R2/V8 MOV 提取 + 信封解码 == 直接解码逐位一致。
- **门禁**：ctest 84/84；pytest media+native+shared 全绿（数字见提交）。

## 四、兼容性

- 新 .toos（内层 V7/V8）被旧读端以 `UNSUPPORTED_VERSION` 明确拒绝——
  与 file_version_major 未知 major 硬失败同语义；
- 既有 .toos（内层 V1..V6）合法性零变化（纯接受域扩展）；
- golden .toos（内层 V1）不变——conformance 门禁保持绿。

## 五、序列帧实测——产品档位口径（bench_toos_sequence.py / toos_sequence_tiers_2026-09-12.json）

真实素材 8 帧序列、ADR-I011 档位（qm/qp 同 `image_cli` kTierPresets）、
AQ 开；解码 = 生产绑定路径逐帧（probe + PIXL 读 + 帧解码）。
尺寸/帧 MB（rans2 新默认）与解码速度：

| 档位 | 2K（prores/dnxhr） | 4K | 8K | 4K 解码 | rans2 vs vlc（4K 均值） |
|---|---|---|---|---|---|
| 422-low    | 0.26 / 0.74 | 0.94 / 2.50 | 3.07 / 6.85 | 9.7~13.7ms（68~99fps） | 尺寸 −9.4%，解码 ±0 |
| 422-medium | 0.34 / 0.90 | 1.25 / 2.86 | 4.10 / 8.35 | 10.8~14.0ms（71~91fps） | 尺寸 −9.0%，解码 −8.1% |
| 422-high   | 0.47 / 1.15 | 1.83 / 3.37 | 6.00 / 10.83 | 12.4~15.2ms（59~81fps） | 尺寸 −9.0%，解码 −3.9% |
| 422-ultra  | 0.97 / 1.97 | 3.90 / 5.04 | 12.00 / 17.81 | 18.0~20.2ms（49~54fps） | 尺寸 −7.3%，解码 −10.0% |

- 2K(1080p) 实测与 CLI 档位锚点（~1.0/1.3/1.7/2.9 MB@1080p）同量级；
  绝对值随素材特性浮动（prores 422HQ 素材细节软，低档位收得更快）。
- **编码速度**（tc_frame_encode 纯帧编码，双素材均值，rans2）：
  2K 4.6~9.4ms/帧（112~206fps）、4K 15.7~25.3ms（40~60fps）、
  8K 53.2~91.7ms（9~17fps）；全档位 2K/4K 高于实时。rans2 编码代价
  vs vlc = +13~49%（分辨率越高/档位越低越小），绝对值均为几十 ms 量级。
- 8K 解码 40~72ms（14~24fps）；连续回放仍应走 MOV 视频路径，
  .toos 定位为帧级交互/单帧提取/归档。
- 参考口径（非档位）：qp24 锚点见 git 历史 `bench_toos_sequence.py`
  早期版本（tools/toos_sequence_2026-09-12.json）。
