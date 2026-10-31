# F6.6 · 项目代理与渲染缓存管理流程 — 验证台账

- 日期：2026-10-24；计划条目：§F6.6
- 测试：tests/media/test_f66_proxy_purge.py（5）＋既有代理套件 91 全绿

## 1. 现状核验（56 个代理测试文件）

生成/删除代理、格式/目录/分辨率/容量（DiskProxyConfig codec/resolution
策略）、manifest 状态机（MISSING/GENERATING/READY/STALE/FAILED + 源
mtime/size/策略/版本变化自动失效）、磁盘回调同步、优先级——既有覆盖
厚实。**缺口 = 安全清理**：无 purge API（"不能误删原片/归档/其他工程
必需文件"无实现锚）。

## 2. 交付

| 组件 | 内容 |
|---|---|
| 安全清理 | `AssetProxyManager.purge_proxies`：只删 **manifest 登记的 proxy_path**（未登记文件绝不删）；路径必须位于代理目录内（**逃逸拒绝**——归档/他工程文件保护）；GENERATING 跳过（**活动任务受保护**，播放中清理不炸任务）；`only_if_source_missing` 孤儿模式（源在=跨项目引用可能，不动）；原片路径全程只读 |
| 占用统计 | `proxy_usage_report`：条目总数/状态分布（小写归一）/代理文件字节占用——"位置/配额变化后占用统计一致"的基础 |
| 逐项返回 | removed / skipped_active / skipped_not_found / skipped_outside_dir——清理可解释 |

## 3. 验收 → 证据

| 验收 | 证据 |
|---|---|
| 不能误删原片/归档/其他工程必需文件 | `test_purge_removes_only_registered_proxies`（同目录未登记文件不删+原片在）+ `test_path_escape_outside_proxy_dir_refused`（代理目录外路径拒绝） |
| 活动文件受保护 | `test_generating_entries_protected`（GENERATING 跳过） |
| 跨项目引用受保护 | `test_orphan_only_mode_keeps_referenced`（源在不动，源失才清） |
| 占用统计一致 | `test_usage_report_counts`（总数/状态分布/字节） |
| 播放中清理正确失效/回退 | 既有 STALE/invalidation 语义（源变化自动失效）+ GENERATING 保护；渲染缓存（render_cache）效果变化指纹失效为 F4.1 批已锚 |
| 批量生成取消/失败重试可解释 | 既有 `_mark_failed`（错误记录）+ GENERATING 生命周期；ensure_proxy_async 队列语义既有 |
| 导出原片/缓存策略可见 | render_cache 消费策略既有（preflight/预览降级路径） |

## 4. 登记的后续片（不阻塞验收）

1. 媒体池 UI：清理入口（按工程/选中/失效/孤儿四档）+ 占用面板。
2. 目录迁移/低磁盘策略的设置面板接线（DiskProxyConfig 既有键）。
3. F6.2 代理/原片重连与 purge 的 UI 联动。
