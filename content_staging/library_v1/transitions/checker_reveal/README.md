# X03 checker_reveal —— 二维交错棋盘揭示

> 状态：**component_ready**（暂存独立 CPU 核；未接宿主）。
> 台账：L00 §2 X03 行——二维交错推进无实现（棋盘资产均非转场，
> composite_viewport.py:11408 为无关资产对照）；差量表 §5"二维交错推进，
> 不是单幅 wipe"。

## 算法（语义修正版）

```
格 (r,c)：r = floor(y·rows), c = floor(x·columns)   # y=(j+0.5)/H, x=(i+0.5)/W
trig(r,c) = mod(((r+c) % 2)·0.5 + phase, 1)          # 触发点（棋盘奇偶两波）
mask = clamp((p − trig)/max(soft,1e-6), 0, 1)
```

- **棋盘交错**：(r+c) 偶格 trig=phase、奇格 trig=phase+0.5——相邻格（横竖
  皆然）恒错相，p 中段同行内 0/1 交替、翻转次数 ≥ 格列数−1；与单幅 wipe
  （单一标量边界、一行至多一次翻转）机器可辨。phase=0.5 交换奇偶两波。
- **交接冲突记录**：初版 trig = (r/rows + c/columns)/2 为反对角波次场——
  同行格触发点单调递增，p 中段一行只有一次 0/1 翻转，实为"格状对角
  wipe"而非棋盘交错（交付自带的交错语义测试当场抓出）。修正为奇偶两波；
  rows/columns 只控制格尺寸、不参与触发序。README/核/手算向量三者已
  同步重算。
- p=0 → trig≥0 → 全 0；p=1 → 早退全 1（trig < 1，早退保证比特级）。
- p 单调：trig 逐像素固定，mask 随 p 线性不减。
- 坏参数防御：rows/columns 取整 clamp [1,16]、phase mod 1、softness
  clamp [0,0.5] + 1e-6 守护；NaN/Inf 回退默认，不抛。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| rows | 4 | 1–16 int | 行格数 |
| columns | 4 | 1–16 int | 列格数 |
| phase | 0 | 0–1 | 触发点场相位偏移 |
| softness | 0.05 | 0–0.5 | 前沿羽化宽（进度域） |

## 交付物

| 文件 | 内容 |
|---|---|
| `checker_reveal_core.py` | 独立 CPU mask 核（numpy float64 → float32） |
| `hand_vectors.json` | 7 组手算向量（波次/羽化 0.5 精确/phase/非方格/端点） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |

## 验证现状

- CPU 核 ↔ 手算向量一致（独立脚本纯 Python 重推）。
- 语义冲突与解决：初版反对角波次场非棋盘（见算法节冲突记录），交付自带
  的交错语义测试当场抓出；修正后全部向量按奇偶两波语义重算。
- **未验**：GPU spec/GLSL/MSL 三端（L03 mask 子门）；真实命令/预览/导出链。
