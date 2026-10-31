# ADR-I018：ROI/TILE 与超大图片路线决策

## 状态

评估完成，TILE major implementation deferred（O5，2026-09-08）。

## 事实

当前 TPIM v1 capability 明确报告：`tile_roi=false`、`multi_image=false`，
最大 coded dimension 为 16384、最大 PIXL 为 256 MiB。O3 的 target-size API
已经统一了预览几何，但当前 native ABI 仍先产生完整平面；因此它不能被当作
8K/超宽图的 ROI 内存方案。

## 决策

暂不修改 TPIM major，也不在没有真实请求分布和 RSS 数据时加入 TILE/TDIR。
当前 `.toos` 继续使用完整帧顺序读取，预览使用 target-size 后缩放；专业查看
和局部检视先通过能力查询明确显示“ROI unsupported”。这样不会让外部宿主误以为
任意 tile 都能独立解码，也不会提前冻结未经验证的 tile 边界、Alpha、色彩和 CRC
语义。

ROI/TILE 重新开启的门槛固定为：

1. O3/O4 在真实 4K、8K、超宽全景和透明大图上给出峰值 RSS、临时分配、I/O
   放大和局部检视请求分布；
2. target-size/full-frame 路径无法在产品预算内满足首帧或 RSS 门槛；
3. 先完成 tile 尺寸、边界扩展、独立 CRC、坏 tile/缺 tile、色彩/Alpha 跨 tile
   语义和 major version 草案，再做单 tile scalar writer/reader；
4. 单 tile 与完整帧的 scalar 参考结果 bit-exact，且截断/坏 tile 错误有界。

## 当前验收与后续入口

- `tests/media/test_topos_image_source.py::TestImageBinding::test_capabilities`
  锁定 v1 不宣称 ROI/TILE。
- `docs/image/ADR-I016-preview-decode.md` 记录 target-size 的 RSS 边界。
- 当门槛满足时，新增 `topos_image_file_spec_v1.md` 与独立 capability/profile，
  不覆盖 v1 reader 的既有语义。
