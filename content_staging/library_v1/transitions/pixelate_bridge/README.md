# X13 pixelate_bridge —— 像素化桥接（sample 双源）

> 状态：**component_ready**（暂存独立 CPU 核；未接宿主）。
> 台账：L00 §2 X13 行——动态量化时序（块内采样变粗→换源→恢复）无等价实现；
> 差量表 §5"动态量化时序，区别普通 pixel filter；进出端点恢复精度"。

## 算法

```
In  前半（p<switch）：c_eff = cell·(p/switch)          # A 逐级粗化
    后半（p≥switch）：c_eff = cell·(1−(p−switch)/(1−switch))  # B 逐级细化
Out 前半：c_eff = cell·(1−p/switch)                    # 镜像剖面：峰值在两端
    后半：c_eff = cell·((p−switch)/(1−switch))
src = A（p<switch）/ B（p≥switch）；c_eff = max(c_eff, 1)
块中心采样（Pixel_Blocks 同族）：块 b = floor((i+0.5)/c_eff)，
    src 列 = clip(floor((b+0.5)·c_eff − 0.5), 0, n−1)；c_eff=1 → 恒等
```

- **In**=粗化峰在 switch（A 粗化进入→换源→B 细化退出）；**Out**=时间镜像
  （switch 处最细 c_eff=1 → B 恒等帧；两端最粗）。任务文本未定义 Out 细节，
  此为已记录裁决（镜像剖面，端点不受影响）。
- p=0 → A 比特级原样；p=1 → B 比特级原样（早退 + c_eff=1 恒等采样双保险）。
- source_switch clamp [0.01,0.99]（除零守护，条数极值同款纪律）。
- uint8 进 uint8 出、float 进 float32 出（镜像 push_frame 约定）；
  灰度 (H,W) 与 (H,W,C) 通道图均适用。
- 坏参数防御：cell 取整 clamp [4,64]、direction 非法回退 "In"；
  NaN/Inf 回退默认，不抛。双源形状不一致 → ValueError（结构性错误，
  与 X09 true_push 壳同口径）。

## 参数

| 参数 | 默认 | 范围 | 说明 |
|---|---|---|---|
| cell | 16 | 4–64 px | 峰值块尺寸 |
| source_switch | 0.5 | 0–1 | 换源相对进度位置（clamp [0.01,0.99] 防除零） |
| direction | "In" | In/Out | 粗化剖面方向（In=峰在 switch；Out=峰在两端） |

## 交付物

| 文件 | 内容 |
|---|---|
| `pixelate_bridge_core.py` | 独立 CPU sample 核（`evaluate(a, b, p, params)`） |
| `hand_vectors.json` | 9 组手算向量（块中心采样逐级、switch 换源、Out 镜像、垂直块、端点比特级） |
| `component_manifest.json` | L01 契约（ComponentManifest 构造+validate） |

## 验证现状

- CPU 核 ↔ 手算向量一致；冲突记录：c_eff=3 向量初版误记块中心值
  {20,40,70}（下标误算），独立脚本重推为 {20,50,80}（src={1,4,7}）。
- **未验**：GPU spec/GLSL/MSL 三端；L03 重采样子门（双源 UV 重采样通道
  现不存在）；真实命令/预览/导出链。
