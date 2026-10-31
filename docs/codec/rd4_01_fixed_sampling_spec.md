# RD4-01 scalable base 固定采样规范

状态：参考实现已落地，2026-09-08。

## 几何

给定 luma 源尺寸 `(Ws,Hs)` 和 `max_dim=2048`：如果两边都不超过上限，base 保持原尺寸；否则长边设为 `max_dim`，短边使用整数下取整比例 `floor(short × max_dim / long)`，最小为 1。因此 base 长边永不超过 2048，且几何结果不依赖平台浮点实现。

平面几何沿用 Topos 既有语义：YUV 4:2:2 的 U/V 为 `(ceil(W/2), H)`，YUV 4:4:4 与 GBR 为 `(W,H)`。`left=0`、`center=1`、`topleft=2` 作为码流 chroma siting 标签保留；GBR 无色度平面，只接受 `left` 标签。base 缩放始终在各平面自己的采样网格内进行，不把 U/V 当作 luma 再重采样，因而不会丢失 4:2:2 的相位语义。

## 下采样

`tc_base_downsample_u16` 使用精确矩形面积平均。目标像素 `(x,y)` 覆盖的源像素重叠长度用整数有理数计算，横向分母为 `Wd`、纵向分母为 `Hd`，总分母为 `Wd×Hd`；结果采用非负 half-up 四舍五入。该规则对非整倍数和奇数尺寸也成立，并天然只读取源可见区域。

输入样本按 `[0, 2^bit_depth−1]` 防御性钳位；支持 1–16 bit，16 bit 上限为 65535。Alpha 与颜色平面使用完全相同的整数规则，Alpha 的 lossless/full 语义由后续 residual 路径单独保证。

## 上采样

`tc_base_upsample_u16` 使用 half-pixel 双线性坐标：

```text
src_x = (x + 0.5) × Ws / Wd − 0.5
src_y = (y + 0.5) × Hs / Hd − 0.5
```

坐标在 `[0, Ws−1]` / `[0, Hs−1]` 上 clamp，边界采用复制延拓；四个乘积权重在整数分母 `2×Wd` 与 `2×Hd` 下直接累加，最终 half-up 四舍五入并钳到位深上限。禁止调用平台私有缩放器或浮点 fast-math。

## 参考实现和门禁

实现位于 `native/topos_codec/src/transform/base_scale.c/.h`；`test_base_scale` 覆盖 10/12/16 bit、奇数/非整倍数几何、4:2:2 plane width、GBR siting 拒绝、面积平均、half-pixel 双线性、边缘复制和 stride 哨兵。由于参考实现完全是整数运算，x86/ARM/SIMD 优化版必须逐像素匹配它；RD4-02/03 不得自行引入第二套 rounding 或 chroma geometry。
