# F8.3 · 多 take 候选/Audition — 验证台账

- 日期：2026-10-24；计划条目：§F8.3
- 测试：tests/edit/test_f83_audition.py（9）全绿

## 1. 现状核验

通用多 take（时间线片段级）此前不存在（multicam 是源素材分组语义、
ADR take 是素材级、F4.2 是栈级调色版本——三者均非片段级多源候选）。

## 2. 交付

| 组件 | 内容 |
|---|---|
| 候选容器 | `audition.py::Audition`：时间线位置锚定 + 多 take（各保留源窗/素材身份/标签/offline 标记）+ 活动 take 指针；序列化往返 |
| 切换 | `switch_take`（**非破坏性**——其余候选保留）：**ripple**（更长 take 邻片后移量可预览/更短前移）或 **fit**（裁到现时长 fitted 标记）放置策略显式；同长度 same_length 零扰动；混合 fps 候选时长按时间线 fps 换算 |
| 缺源 | `mark_take_offline`：offline 候选不可激活（其他不受影响）；**活动 take 缺源自动回退首个可用**（全 offline → active=None 容器仍在不丢） |
| 拆平 | `flatten_audition`：活动 take 固化 + **其余候选保留**（写回素材池/工程表——明确 flatten 不丢候选）；无活动 take 拒绝 |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 同位置切换不同长度 take，时间线/邻片变化可预览 | ripple shift=+24/-12 可预览量 + fit fitted 标记 + same_length 零扰动（4 测试含混合 fps 换算） |
| Undo/保存重开/成片取正确候选 | 纯数据模型（序列化往返测试）+ 切换是数据替换（命令层包裹即 Undo，F4.2/F8.1 同模式）；成片读活动 take（渲染经片段引用单一真相） |
| 交换明确 flatten | flatten 返回 (活动, 其余)——显式操作不静默 |
| 缺源不丢其他候选 | offline 不可激活 + 活动回退 + 全 offline 容器保留（3 测试） |
| 与调色版本/ADR 区分 | 时间线片段级多源候选（三重区分声明：F4.2 栈级/ADR 素材级/Audition 片段级） |

## 4. 登记的后续片（不阻塞验收）

1. UI：片段右键"转为 Audition"/take 切换面板/拆平入口（模型已就绪）。
2. 工程 ``auditions`` 字段 + IO settings 接线（模型序列化已就绪）。
3. 预览行为（take 预览时临时替换解码源）经渲染层消费接线。
