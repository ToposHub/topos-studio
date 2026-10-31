# X05 luma_reveal —— 内容驱动亮度揭示

> 状态：**component_ready**（暂存独立 CPU 核；未接宿主）。
> 台账：L00 §2 X05 行——现有 33 转场 mask 全几何（transition_mask.py 全文），
> 需把源图像亮度接进 mask 源（L03 内容源接缝）；差量表 §5"以图像内容驱动；
> 常量亮度和 HDR 输入仍有确定端点"。

## 算法

```
luma = Rec.709 加权（灰度输入直取）；nan/±inf 折叠后 clamp01（HDR 折叠到 1）
luma_eff = clamp01(luma + (0.5 − threshold))     # threshold=亮度偏置参考（0.5 不偏置）
luma_eff = 1 − luma_eff                           # invert：反相排序对象
thr_eff = mix(−span, 1+span, p) = −span + p·(1+2·span)
mask = clamp((thr_eff + span/2 − luma_eff)/span, 0, 1)
```

- **A 从暗到亮揭示**：luma_eff 低于移动阈值先揭示；source 选 A/B 图做
  排序源。p=0 → thr_eff=−span（luma_eff≥0 全不揭示）、p=1 → 1+span
  （luma_eff≤1 全揭示）——**常量亮度输入端点确定**（早退再给比特级）。
- p 单调：luma_eff 逐像素固定，mask 随 p 线性不减。
- **设计冲突记录（invert）**：任务原文"mask … 或 invert 反相"若反相 mask
  本体，则 p=1 → 全 0，违反端点精确纪律——故 invert 反相的是**亮度排序**
  （先亮后暗），端点语义不变。这是规格歧义的已记录裁决。
- **设计裁决（threshold）**：progress 已把有效阈值扫满 [−span,1+span]，
  threshold 参数作为亮度偏置参考点（0.5=不偏置；>0.5 提升暗部优先级），
  单调且不动端点。
- **缺省回退（声明）**：image（按 source 取 A/B）为 None、形状与
  (width,height) 不符、或通道异常时，回退**线性 dissolve mask=progress**
  （端点仍精确），不抛。
- 坏参数防御：source 非法回退 "A"、threshold clamp [0,1]、span clamp
  [0.01,0.5]；NaN/Inf 回退默认。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| source | "A" | A/B | 亮度排序源 |
| threshold | 0.5 | 0–1 | 亮度偏置参考点（0.5=不偏置） |
| span | 0.2 | 0.01–0.5 | 软阈宽（前沿亮度跨度） |
| invert | false | bool | 亮度排序反相（先亮后暗） |

## 交付物

| 文件 | 内容 |
|---|---|
| `luma_reveal_core.py` | 独立 CPU mask 核（`evaluate_mask(w, h, p, params, image_a=None, image_b=None)`） |
| `hand_vectors.json` | 8 组手算向量（前沿 0.5 精确/invert 镜像/偏置/源 B/缺省回退/端点） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |

## 验证现状

- CPU 核 ↔ 手算向量一致（独立脚本纯 Python 重推，无冲突）。
- **未验**：GPU spec/GLSL/MSL 三端；L03 mask 子门+内容源接缝（mask 现状
  无图像输入通道）；HDR >1 输入的中段行为折叠为 1（显示域 tonemap 属宿主）。
