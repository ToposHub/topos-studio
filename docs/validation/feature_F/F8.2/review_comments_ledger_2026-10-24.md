# F8.2 · 带版本锚点的审片评论与 marker — 验证台账

- 日期：2026-10-24；计划条目：§F8.2
- 测试：tests/edit/test_f82_review_comments.py（12）全绿

## 1. 交付

| 组件 | 内容 |
|---|---|
| 评论模型 | `review_comments.py::ReviewComment`：稳定 id + 作者 + 状态（open/resolved/rejected 校验）+ **版本锚点**（version_hash=F8.1 快照哈希）+ 三重锚（timeline_frame / asset_id+source_frame / clip_id）+ 回复链 |
| 导入导出 | JSON 往返（schema topos.review_comments/1，Unicode 安全 ensure_ascii=False）；**重复导入幂等**（稳定 id 去重）；**隐私清理**（strip_authors 剥离作者）；未知 schema 拒绝 |
| 评论→marker | open→红/resolved→绿/rejected→灰；空正文不生成 |
| **重定位** | `relocate_comment`（置信度分级）：**exact**（1.0）素材+源帧双匹配迁移（源锚跨 fps 不变——改 fps 同素材 exact）；**timeline_only**（0.5）仅无源锚纯时间线评论帧界内保留（近似声明）；**stale**（0.0）素材被删/源帧被修剪→标失效——**有源锚的评论绝不静默挪到别的镜头**（计划原文） |

## 2. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 客户第 N 版评论定位正确 | exact 迁移测试（源帧双匹配→新时间线帧/clip 绑定） |
| 重复导入幂等 | `test_roundtrip_unicode_idempotent_reimport`（二次导入 added=0） |
| 改 fps/重套/删除镜头后可解释映射或标失效 | exact（同素材改 fps）/ timeline_only（无锚近似 0.5）/ stale（删镜头 0.0 不挪、源帧出窗 0.0 不挪）四测试 |
| Unicode/时间码/隐私清理 | Unicode 往返 + strip_authors 测试 |
| 不默认部署云服务 | 文件交换即接口（计划原文遵守） |
| Undo/保存恢复 | 评论为纯数据模型（序列化往返）；经命令层包裹即得 Undo（F4.2/F8.1 同模式） |

## 3. 登记的后续片（不阻塞验收）

1. 审片面板 UI（导入/导出/状态流转/重定位报告展示——模型已就绪）。
2. 评论→timeline marker 的命令级接线（marker 载荷生成已就绪）。
3. AI 工件接口消费（F0.2 消费者清单再核）。
