# F09 dust_scratches —— 程序灰尘/划痕（L12，静态程序版）

> 状态：**component_ready**（暂存组件；未接宿主，不在产品菜单）。
> 台账：L00 §1 F09 行——全库无灰尘/划痕类（无重复）。程序静态版先行；
> **外接纹理版依赖资源子门 + M20 闭包，不在本批**。

## 算法

```
格子：cell = max(size,1) 输出像素；格 id (ix,iy)=floor(像素中心/cell)，
      格内局部 f；uint32 murmur3 风格散列 h32（同 F07/F08 常量族）
尘点（格内小椭圆暗斑，程序 SDF）：
  h0 = h32(格, s) < density → 含尘点（概率∝density）
  斑心 c = (0.3+0.4·hu, 0.3+0.4·hv)（相位偏移 (3,11)/(7,5)/(13,2)）
  e = ((fx-cx)/0.20)² + ((fy-cy)/0.26)²
  t = clamp((1.2-e)/0.5, 0, 1)；spot = t²(3-2t)；深度 = 0.4+0.4·hd
  （斑完全落于格内：c∈[0.3,0.7]²、半径 ≤0.26 → 格界处 e≥1.2）
划痕（格列散列选中列的竖向细线）：
  h32((ix,917), s) < density·0.05 → 该列含划痕（密度门与尘点共享）
  span = clamp(scratch_length,0,1)；y0 = hl·(1-span)；带 = [y0, y0+span]（uv.y）
  线位 xs = 0.25+0.5·hxs；半宽 w = 0.10+0.08·hw（格内比例）
  prof = 1-q²(3-2q)，q = clamp(|fx-xs|/w,0,1)；端部 0.03 归一羽化
衰减（乘性，HDR/黑位安全）：atten = opacity·深度·spot + opacity·0.35·prof·edge
  （mask 域合计钳 0.9）；out = color·(1-atten)
```

- **恒等**：`density=0` 恒等（密度门同管尘点/划痕，早退+oracle 同阈值镜像）；
  `opacity=0` 数学恒等（乘 0）；`scratch_length=0` 关闭划痕（span 门）。
- **加暗语义**：乘性衰减——黑位保持黑、HDR 等比衰减、基色不 clamp
  （mask 域钳 0.9 是显式声明的权重域守护）。
- **静态限制（L01 冻结口径）**：seed 滑杆（0..100 整数语义）决定全部格点
  散列，同一 seed 每帧相同；**动画（尘点闪烁/划痕游移）依赖正式 frame/seed
  子门，落地前不声明**。
- **资源边界**：本版纯程序（零纹理、零循环、零邻域采样）；
  外接扫描纹理版过资源子门+M20 闭包后再议，不占本批。

## 交付物

| 文件 | 内容 |
|---|---|
| `dust_scratches.tglsl` | 源码草稿（uint 散列 + 椭圆 SDF + 列划痕；零循环零采样） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |
| `hand_vectors.json` | 9 条手算向量（16×16 白图夹具，独立脚本推导，容差 1e-6） |
| `references/dust_scratches_ref.py` | CPU oracle（float64，散列/SDF/羽化逐式同构） |

## 验证现状

- CPU oracle ↔ 手算向量一致（tests/content_library_v1/test_f09_dust_scratches.py）。
- 真实 parser：`PluginManager()._parse_plugin_file` 显式暂存路径解析通过。
- GLSL 红线静态检查：无 while/无循环/u_* 白名单（textureSize 取尺寸）。
- **未验**：真实 GPU 编译/像素（L02 子门）；Metal/CPU 导出；
  动画 frame 契约；外接纹理版（资源子门）。
