# X04 staggered_tiles —— 分块错峰缩放桥接（L14）

> 状态：**component_ready**（暂存 CPU 核；未接宿主，不在产品菜单）。
> 台账：L00 §2 X04 行——每块独立时序 + UV 真变化，与 X03（mask 类块状揭示）
> 的机器可辨差量是**采样坐标真移动**；需双源重采样通道（L03 子门，X09 已立）。

## 算法

```
g×g 网格（grid），块 (ix,iy)，行主序号 k = iy·g + ix：
  rank：Row    → k/(g²-1)                 （行序：上行先动，行内左→右）
         Random → hash(seed,k) 升序名次/m （m=g²，uint32 murmur3 家族）
  delay_i = delay · rank；delay_max = delay · max(rank)
  块有效进度 pi = clamp((progress - delay_i) / (1 - delay_max), 0, 1)
    —— 所有块恰在 p=1 同时到 pi=1、rank=0 的块 p=0 即启动（结构保证端点）
块内（局部坐标 ℓ ∈ [0,1)²，块中心采样）：
  A 层：窗 scale sA = 1 - scale_amt·pi（≤1 → 恒在块内）
        src = (块原点 + 0.5 + (ℓ-0.5)·sA) / g
  B 层：窗 sB = 1/(1 - scale_amt·(1-pi))（自宽窗收缩到 1，可越块 → clamp）
  out = (1-pi)·A_layer + pi·B_layer
```

- **块间硬边**：相邻块 pi/窗不同 → 采样场在块界不连续（按参数语义**不羽化**，
  差异可见即"分块"读法的一部分）。
- **端点**：p=0 → pi≤0 全块 → A 原位（A 层窗=1 恒等采样）比特级；p=1 →
  pi=1 全块 → B 原位（B 层窗=1）比特级。early return 直拷。
- **单调**：pi 逐块随 p 单调不回退（clamp 公式）。
- **防御**：数值参数 NaN/Inf/非数回退默认、越界钳制；order 非法值 ValueError；
  双源尺寸不一致 ValueError；uint8 输入返回 uint8（round+clip）。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| grid | 4 | 2..8（int） | 每轴块数（g×g 块） |
| delay | 0.2 | 0..0.5 | 首末块最大错峰延迟（归一 progress） |
| order | Row | Row/Random | 块启动顺序（行序 / hash 排序） |
| seed | 0 | 0..100（int） | Random 档散列种子 |
| scale_amt | 0.25 | 0..0.5 | 块内采样窗收缩量（A 窗 1→1-scale_amt） |

## 交付物

`staggered_tiles_core.py`（CPU 核，统一签名 evaluate）/ `README.md` /
`hand_vectors.json`（3 组向量 + 冲突 2 条）/ `component_manifest.json`。

## 验证现状

- CPU 核 ↔ 独立标量手算脚本全帧对拍（Row/Random×2，max diff ≤ 3e-8）；
  端点比特级（float32/uint8）；错峰时序/块内缩放/Random seed 有效性测试通过。
- **未验**：GPU spec/GLSL/MSL、真实预览/导出链（L03 重采样子门集成后补）。
