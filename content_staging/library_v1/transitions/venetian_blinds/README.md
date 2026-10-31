# X02 venetian_blinds —— 周期百叶条带揭示

> 状态：**component_ready**（暂存独立 CPU 核；未接宿主）。
> 台账：L00 §2 X02 行——现有唯一 mask 边界是 wipe 单边界（transition_wipe.py:144-147），
> 无周期条带；差量表 §5 X02 行"多条带揭示；方向仅配方，条数极值不除零"。

## 算法（语义修正版）

```
x=(i+0.5)/W, y=(j+0.5)/H；aspect=W/H；A=radians(angle)
s = x·cosA + (y/aspect)·sinA          # 投影轴坐标（宽度单位；aspect 校正同 X01 族）
t = mod(s·count + phase, 1)           # 带内相位 ∈ [0,1)；条带边界在 s·count+phase ≡ 0 (mod 1)
mask = clamp((p − t)/max(soft,1e-6), 0, 1)
```

- **同时扫掠语义（经典 blinds）**：全部条带共享同一带内相位扫掠——任一
  带内位置 t 的像素在 p=t 时开始揭示、p=t+soft 处完成；条带间无错峰。
- **交接冲突记录**：初版公式 r = mod(s·count+phase, count) 在 s∈[0,1) 内
  除最末带外不回绕，(p·count − r) 数学上退化为"带角度线性 wipe"（count
  只改斜率不改图案，条带完全不错峰），且最末带附近存在回绕跳变——独立
  语义测试（count 变化输出不变）当场抓出。修正为上式；README/核/手算
  向量三者已同步重算。
- 像素各向同性：angle=0 带宽 W/count px；angle=90 带高 H·(1/aspect)/count
  = W/count px（8x4 aspect=2 实测带高=带宽=2px）。
- p=0 → 全 0；p=1 → 全 1；**端点早退比特级**。
- p 单调：t 逐像素固定，mask 随 p 线性不减。
- angle=0 竖直条带带内自左向右；angle=90 水平百叶带内自上而下；负角向下-左。
- 坏参数防御：count 取整 clamp [2,32]（条数极值不除零）、angle clamp ±90、
  phase mod 1、softness clamp [0,0.5] + 1e-6 守护；NaN/Inf 回退默认，不抛。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| count | 6 | 2–32 int | 条带数（沿投影轴铺满 [0,1]） |
| angle | 0 | −90–90 度 | 条带角（0=竖直条带；正角向屏幕下方转） |
| phase | 0 | 0–1 | 条带图案相位（mod 1） |
| softness | 0.05 | 0–0.5 | 前沿羽化宽（带内相位单位） |

方向变体（自左/右揭示）不另算家族（台账口径），配方层翻转 angle 即得。

## 交付物

| 文件 | 内容 |
|---|---|
| `venetian_blinds_core.py` | 独立 CPU mask 核（numpy float64 → float32；L13 集成时评估并入生产 cores.py） |
| `hand_vectors.json` | 7 组手算向量（错峰/羽化 0.5 精确/angle=90/phase/端点；独立脚本推演） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |

## 验证现状

- CPU 核 ↔ 手算向量一致（独立脚本：纯 Python/math 重推，不复用核代码）。
- 手算冲突与解决（两轮）：①初版 angle=90 向量误用 p=0.5（旧语义下会全亮），
  独立脚本重推改 p=0.3；②初版核公式整体退化为线性 wipe（见算法节冲突
  记录），全部向量按修正语义第二轮重算。
- **未验**：GPU spec/GLSL/MSL 三端（L03 mask 子门）；真实命令/预览/导出链。
