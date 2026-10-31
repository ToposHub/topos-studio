# ADR-I016：Topos Image 目标尺寸预览解码

## 状态

已接受（O3 target-size native reconstruction，2026-09-08）。

## 决策

`ToposImageCodec.decode(path, target_size=(w, h))` 现在提供统一的预览尺寸
语义：保持原始宽高比、仅缩小不放大，并按像素格式调整平面几何。GBR 与
YUV 4:4:4/Alpha 使用目标可见尺寸，YUV 4:2:2 的 U/V 平面使用目标宽度的
向上取整一半；Alpha 始终保持全分辨率。缩放索引是确定性的最近邻采样，避免
在绑定层引入额外图像库。

文件浏览器的 `.toos` provider 在解码前把 small/large/preview 的包围盒传给
binding；WebP 编码仍保持原子替换。缩略图 sidecar 从旧的 `mtime:size` 文本
升级为版本化 JSON，记录源文件 mtime、size 和 SHA-256，同时继续读取旧格式。

## 已知边界

当前 `tc_image_decode_preview` 已把 target 尺寸下推到 packet 解码核心：颜色
slice 在反量化后只计算目标网格实际采样到的块内像素，不分配完整源平面；V3/V6
空间预测和 alpha 预测仍需顺序重建源块/源行状态，但只把目标采样点写入输出。
因此它是真正的 target-size reconstruction，能显著降低预览输出内存与 IDCT 像素
写回量；码流熵语法仍须顺序读取并校验全部 slice，所以不是“跳过 8K 压缩包读取”。
性能收益需按 profile、缩小比例和素材熵分布基准化，不能直接承诺固定倍数。

## 验证

- binding 回归覆盖 GBR、YUV422+Alpha、宽高比和非法尺寸。
- native `test_image_reader` 覆盖目标平面几何、最近邻采样值和 warning 透传。
- native `test_transform` 覆盖 sampled inverse 与完整逆变换逐位一致。
- `docs/image/reports/target_decode_o3.json` 记录了 4K→1K target-size 与完整解码
  的 RSS/耗时对比；该报告不替代 8K/12K 真机基准。
- file explorer 回归覆盖 provider target-size 传递、WebP 输出和 sidecar hash。
- 旧 sidecar 仍按原规则读取，避免升级后强制全量重建。
