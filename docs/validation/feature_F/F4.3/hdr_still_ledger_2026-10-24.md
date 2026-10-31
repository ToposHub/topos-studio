# F4.3 · Lightbox、Gallery 与 HDR 参考 still — 验证台账

- 日期：2026-10-24；计划条目：§F4.3
- 测试：test_presets_gallery.py（34，含 F4.3 新 6）+ test_ab_reference_wipe.py（14，含 F4.3 新 2）全绿

## 交付

| 组件 | 内容 |
|---|---|
| HDR still 存储 | `preset_meta.write_still_exr/load_still_exr`（cv2 EXR FLOAT32+ZIP；import cv2 前置 OPENCV_IO_ENABLE_OPENEXR 同 main.py 模式）；`{name}.still.exr` sidecar |
| schema 2 | `PresetMeta` 新增 `still_precision`（float32/half/None=legacy）、`working_space`、`display_transform`（锁定 ODT 描述）；`from_dict` 忽略未知字段双向兼容 |
| 精度声明 | `describe()`：HDR still 标注精度；**旧 JPEG 标明"8-bit JPEG（精度受限参考，非 HDR）"不伪称** |
| Gallery 一致性 | `_SIDECAR_SUFFIXES` 纳入 `.still.exr`——rename/delete/移动与既有 sidecar 行为一致（测试锁定） |
| difference 对比 | `SPLIT_VIEW_FRAGMENT` 新增 `u_compare_mode`（全屏 \|current−reference\|，黑=一致）；engine `set_compare_mode` 状态机（复用参考纹理通路，未知模式归一 split）；canvas 直通 |
| JPEG 兼容 | 旧 still.jpg/thumb.jpg 路径零改动（缩略图继续 JPEG——缩略图不冒充全精度） |

## 验收 → 证据

| 验收 | 证据 |
|---|---|
| 超 1.0 高光/负值/渐变保留 | `test_exr_roundtrip_preserves_hdr_values`（4.5/-0.25/12.0/渐变逐值往返） |
| 旧 JPEG 标明精度限制 | `test_legacy_jpeg_precision_declared` |
| 批量 rename/delete/移动/缺 sidecar 一致 | `test_rename_delete_move_exr_sidecar` + 存量 rename/delete sidecar 测试 |
| wipe/split/difference | 差异模式 shader + 状态机 2 项；split/A-B 为存量（既有测试族） |
| 高精度参考声明（工作空间/传递函数/显示语义） | schema 2 roundtrip 测试（working_space + display_transform[tonemap/target_peak]） |

## 登记的后续片（不阻塞验收）

1. 抓帧端 float 读回：Color 页 `render_cached_image` 在 AUD-0604 处 clip uint8——
   EXR 生产路径需 float readback 分叉（存储/消费已就绪，抓帧仍出 JPEG+EXR 需
   该片）；当前 EXR 可由外部 float 帧写入（API 就绪有测试）。
2. 参考侧显示变换锁定执行：schema 已声明 display_transform；shader 侧参考
   纹理应用锁定 ODT（复用 pq_hdr_to_display 三模式）待片。
3. Lightbox 对话框（多选 2-N 张全精度对比 UI）；difference 模式菜单项接线。
4. alpha 通道 EXR（当前 3/4 通道均可写；4 通道端到端参考上传联验待片）。

## 诚实边界

- difference 在显示域 float 上取绝对差（参考与当前同域）；跨域参考需先经
  display_transform 归一（后续片 2）。
