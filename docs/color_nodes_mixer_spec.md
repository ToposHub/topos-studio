# 调色节点 Mixer / Key / Splitter 数学规格（B10 冻结 v1）

日期：2026-11-02。状态：**冻结**（实现依据；变更需 bump `semantic_version`）。
工作域：scene-linear，HDR 不截断（除非显式标注）；key 有界 [0,1]；image alpha 不作为隐含 matte 载体（§6.3）。

## 1. Parallel Mixer

输入：`base`（image，slot 固定）；`branch_i`（image，稳定 slot ID 序）＋
每分支有效 key `k_i`（无 key 边时 `k_i ≡ 1`…见下）。

**有效 key 规则（v1）**：分支未连接 key 边 → `k_i ≡ 1`（全权重）；
分支禁用（enabled=False）→ 该分支跳过。

```
out = base
for i in slots（稳定 slot ID 升序）:
    out = out * (1 - k_i) + branch_i * k_i        # 逐像素 lerp
alpha_out = alpha_base                              # key 不写 image alpha
```

## 2. Layer Mixer（由下到上）

slot 0 = 最底层。有序折叠：

```
out = slot_0
for i in 1..n（slot ID 升序 = 由下到上）:
    out = blend(out, slot_i, mode_i, opacity_i)
```

v1 blend 集合与公式（straight alpha，HDR 安全）：
- Normal: `out = out*(1-op) + slot_i*op`
- Add: `out = out + slot_i*op`
- Subtract: `out = out - slot_i*op`
- Multiply: `out = out * mix(1, slot_i, op)`
- Screen: `out = 1-(1-out)*(1-slot_i*op)`（scene-linear 下允许超 1）

alpha 同 Parallel（保留基底）。层序是像素语义：重排 slot = 像素变更。

## 3. Key Mixer

```
out = k_0
for i in 1..n:
    add:       out = max(out, k_i)
    subtract:  out = clamp(out - k_i, 0, 1)
    intersect: out = out * k_i
```
模式是节点参数（缺省 add）；key 值域 [0,1]（进入前钳制）。

## 4. Outside

引用上游节点 j 的 **effective key** `k_j`（非重新抠像）：

```
k_out = 1 - k_j                # 补集
image_out = grade(image_in, mask=k_out)   # Corrector 数学，mask 为补集
```

## 5. Splitter / Combiner（RGB 三分/三合）

```
splitter.R = vec4(v, v, v, a)  其中 v = texel.r, a = texel.a   # 灰度载体
combiner   = vec4(R.r, G.g, B.b, R.a)                          # R.a 承载 alpha
```

中性 Corrector 下 split→combine 恒等（golden 锚点）。

## 6. Alpha Output

独立 key 终端：消费显式连接的 key 流，发布单通道 alpha 平面；不改变
主 Output 的 RGB/alpha 语义；导出格式能力预检在 B13。

## 7. 误差与验证

- CPU oracle（N09）实现上述全部公式（float32，精确语义）；
- GL float16 路径对规范化测试值 abs≤2e-3（B00 契约）；
- 恒等断言：split+combine 恒等、全白 key 的 Parallel=分支折叠、
  Layer 序敏感性、改变节点位置不改变像素、slot 重排按规格变像素。

## 8. 已知边界（后续版本）

- premultiplied 分支输入的合成约定（当前 straight）；
- Layer Mixer 的完整 18 模式（v1 冻结上表 5 种；其余在 catalog 标
  pending 直到补公式+oracle）；
- 分支 key 的 blur/clean/gain 后处理时序（§4.1 正式冻结时并入）。
