# content_library_v1 文档

L 计划内容库的使用说明、算法区别、平台交接与发布记录。

## 现有文档

| 文档 | 内容 |
|---|---|
| `content_staging/library_v1/specs/spec_schema.md` | L01 组件契约（七组字段/状态纪律/time_domain 冻结/oracle 与夹具约定） |
| `content_staging/library_v1/specs/L00_ledger.md` | L00 逐项去重台账（F/X/C/P 全量） |
| `content_staging/library_v1/references/oracle_conventions.md` | CPU oracle 约定（float64/scene-linear/禁自证/对拍容差） |
| `docs/validation/upgrade_L/L00_baseline.md` | L00 基线（环境/门控/方法） |

## 面向用户的三类入口（规划中，L05 落地）

- Color/Edit 滤镜（F 项）：经共享 EffectCatalog（`plugin:<id>` / 内置 dispatch）；
- 视频转场（X 项）：经既有转场面板/命令（AddTransition/UpdateTransition）；
- 合成节点（C 项）：经 Composite 节点图（`lib1_*` node_type）。

在对应接缝（L02/L03/L04）通过真实执行验证之前，以上任何组件都**不在**
产品菜单中可见；暂存区状态见各组件 manifest 的 `status` 字段。
