# Files 原生 PyQt6 —— P0 闭环验收记录（2026-11-16 第四批）

对应计划：`docs/plans/2026-11-16-files-native-pyqt6-migration-plan.md`
闭环目标：**找素材 → 看清内容 → 多选 → 拖入媒体池 / 时间线**，并给出可诊断的失败。

本目录的截图是**展示夹具**（固定样本 + 合成样张），用于核对信息层级与布局；
真实目录、真实编码格式、真实播放与 DPI 仍需按下面「未覆盖项」实机补证。
不能用截图冒充端到端验收。

## 1. 本批交付

| 编号 | 交付 | 关键点 |
| --- | --- | --- |
| B01 | `src/features/files/contracts.py` | 与传输无关的 DTO / 错误码 / 能力 / 请求身份；`parse_entry` 兼容旧 `/api/list` 字段，`missing=1` → 离线 |
| B02 | `/api/list` 增补技术字段 + `total/truncated/request_id` | `width/height/codec/missing/duration` 随条目下发；递归 2000 条上限显式 `truncated` |
| B03 | 缩略图沿用既有 `/api/thumb`，版本 + 冷缓存重试 | 可见区优先、128 图上限、失败有界重试；失败不伪装成真实封面 |
| B04 | 原生预览取数与容器 | `/api/file` 原始字节 → 64MB 上限 → 后台 `QImageReader` 解码；图片适配/原尺寸/前后切换/Esc 释放；视频音频转既有播放器 |
| B06 | 字段级元数据写入 | 单文件 `/api/update_meta`；多文件 `/api/batch/meta`（含 comments，`rating=0` 真正落库）；服务确认后才更新模型 |
| B08 | 外部投放异步协调 | `ImportProbeJob` 后台探测（≤4 线程、可取消）→ 主线程复核工程/revision/轨道锁 → `MacroCommand` 单次提交 |
| B10 | 用户级 UI 配置 | `NativeFilesPreferences`（QSettings）保存视图/密度/侧栏/信息栏/排序/默认入口；不写路径与机密 |
| B12 | 能力门控 | `/api/capabilities` + `apply_capabilities`：不支持的能力直接禁用并给原因 |
| B05 | 全库检索 + 扫描控制 | `/api/search` 显式 `total/truncated/offset/limit/request_id`；`/api/scan/cancel` 独立取消路径，不关闭周期增量 |
| B07 | 文件任务服务 | `file_tasks.py` + `/api/tasks/start|status|cancel|list`：job_id、逐项结果、进度、取消、`error/overwrite/rename` 冲突策略；Delete/F2/新建文件夹已接真实任务 |
| B09 | 回收站 + 路径传播 | 恢复/清理支持 id 或原路径；`file_meta`（含后代）、`file_tags`、`collection_items`、`stacks.cover_path`、`ocr_text`、`markers`、目录缓存、缩略图随路径变化同步 |
| B11 | 集合浏览 | 面板“集合”入口 + `/api/collections` / 成员列表（`total/truncated`）；复用后端规则，不复制语义 |
| BX01 | 全库备份 | `/api/library/backup`：`sqlite3.backup()` 一致快照 + `manifest.json` 表行数；组织关系与未知字段随库保留 |
| BX06 | 退役审计 | `docs/validation/retire_audit.md` 列出 WebEngine/Flask 消费者 |
| U03 | 预览容器 | Space/双击图片进浮层；`←/→` 在可见图片间切换；`F` 切换适配/1:1 |
| U04 | 投放反馈 | 接受即提示「正在导入 N 个文件…」；完成「已落轨 X 个片段 · Y 个失败（文件名：原因）」；作废/取消/锁轨分别有独立文案 |
| U05 | 标记编辑 | 评分/颜色/标签/备注可改；混合值显示「混合」且空输入不清空；失败回显原因并回填旧值 |
| U06 | 默认入口切换 | 默认进入原生浏览；「完整素材管理」仍可切换并持久化；借道旧宿主预览不改默认 |
| F13 | 批量改名预览 | 多选→“批量改名…”，模板预览前后名与冲突；冲突由服务加序号，执行后显示处理数与失败原因 |
| F14 | 悬停胶片条 | 视频卡片悬停 350ms 后异步取 `/api/thumb/strip`；离开即停；缓存 24 条；卡片底部绘制并随鼠标画 scrub 线 |
| F15 | 并排比较 | 2–4 张图片→“并排比较…”；`/api/file` 原始字节、后台读取、单滚动区同步缩放/平移，±/适应/100% |
| F16 | 旗标筛选 | 工具栏“全部旗标/已标旗/已拒收”，目录始终可见；不改变源目录与成员 |
| U07 | 按需扩展 | 集合浏览 + 批改名 + 并排比较已接；未整包照搬旧 WebEngine 功能 |

## 2. 自动化测试

```bash
PYTHONPATH=/tmp/topos-native-files-test-deps .venv/bin/python -m pytest \
  tests/files \
  tests/ui/test_native_files_panel.py \
  tests/ui/test_native_files_transport.py \
  tests/ui/test_native_files_metadata.py \
  tests/ui/test_native_files_preview.py \
  tests/ui/test_native_files_prefs.py \
  tests/ui/test_native_files_actions.py \
  tests/ui/test_native_files_p2.py \
  tests/ui/test_native_files_platform.py \
  tests/ui/test_files_native_host.py \
  tests/architecture/test_file_explorer_integration.py \
  tests/ui/test_panel_splitter.py -q
```

本批结果：**171 passed**（含 13 项 API 契约 / 6 项文件任务 / 13 项面板动作 / 7 项 P2 / 4 项平台 / 22 项面板 / 36 项 transport）。
另跑 `tests/ui/test_ef03_aud_part2.py`（外部投放历史审计，已按 B08 新结构更新）：**71 passed**。合计 **242**。

覆盖要点：

- `tests/files/test_contracts.py`：DTO/错误/兼容解析；
- `tests/files/test_files_api_contract.py`：`/api/list` 技术字段、`total/truncated/request_id`、能力清单鉴权、`/api/batch/meta` 的 `rating=0`/comments/字段隔离；
- `tests/files/test_import_job.py`：探测离线程、逐项失败、取消不产生半批、去重、`prepare_import` 复用已存在素材；
- `tests/ui/test_native_files_transport.py`：单/多文件写入路由、失败不发布成功、预览 200/423/204/超大/取消；
- `tests/ui/test_native_files_preview.py`：真实 PNG 解码、Esc 关闭、陈旧字节丢弃、视频转既有播放器、单图禁用步进；
- `tests/ui/test_native_files_metadata.py`：只提交改动字段、混合值不清空、失败不回写模型；
- `tests/ui/test_native_files_prefs.py`：跨面板往返、侧栏/信息栏手动选择粘性、只存 UI 键；
- `tests/ui/test_ef03_aud_part2.py::TestAud21412DropProbePool`：drop 内不再 `.result()`、提交前复核 project_id/revision/锁、单宏、字幕路径仍保留有界线程池；
- `tests/files/test_file_tasks.py`：mkdir/rename 逐项目标、`error/rename` 冲突策略、部分失败不中断整批、取消后 pending 标记 skipped、非法操作拒绝；
- `tests/ui/test_native_files_actions.py`：Delete 发出 trash 意图、无选择不调用服务、F2 带新名称、新建文件夹、部分失败文案、全库结果 total/截断、集合成员显示；
- `tests/ui/test_native_files_p2.py`：旗标筛选、批改名预览/冲突/结果、悬停延迟与离开取消、比较缩放与错误；
- `tests/ui/test_native_files_platform.py`：NFC/NFD 身份、大小写、Windows/UNC 归一、特殊字符 URL 往返、760px 主控件可达。

## 3. A01–A10 状态

| ID | 状态 | 证据 |
| --- | --- | --- |
| A01 | 通过 | 导航/历史/Home 惰性树由 `test_native_files_panel.py` + 既有 focused tests 覆盖；`native_files_p0_inspector.png` |
| A02 | 通过 | 两视图共享选择、排序、过滤、快捷键作用域有测试；`native_files_p0_list.png` |
| A03 | 通过（自动化跨平台逻辑） | `test_native_files_platform.py`：NFC/NFD 同身份、`C:\`/UNC 归一、`# % 引号 空格` 的 QUrl 往返；真实跨系统共享盘人工复核仍由发布流程执行，见 `platform_matrix.md` |
| A04 | 通过 | 晚到响应、取消、断开、错误清空旧条目均有测试 |
| A05 | 通过 | `test_import_job.py` 覆盖去重、逐项失败、取消不产生半批；`test_ef03_aud_part2.py` 证明 drop 立即接受并转后台 |
| A06 | 通过 | 落轨目标/帧计算/锁轨拒绝/无工程语义有领域测试；单宏提交与 AV 复用由 `prepare_import` 断言 |
| A07 | 通过 | 提交前复核 project_id/revision/轨道锁（AST + 领域测试），单宏保证一次 Undo 回滚整批 |
| A08 | 通过 | 图片真实解码/释放/不阻塞 GUI；预览关闭与 `cancel_preview` 释放字节；视频音频走既有播放器（不在本模块建第二套播放会话） |
| A09 | 通过 | 锁目录 423、离线条目、无内容 204、超大文件、解码失败均有明确文案 |
| A10 | 通过（自动化：DPI 1/1.5/2 + 浮动） | `platform_check.py` 三个缩放因子下主控件可达、侧栏/信息栏不裁切、QDockWidget 浮动 700×500 面板可见（`platform.json` / `platform_matrix.md`）；transport `shutdown()` 取消预览/列表/树/任务轮询 |

## 4. 性能与平台证据

- `performance.md` / `bench_ui.py` / `performance.json`：100 / 1,000 / 10,000 条冷热首屏、排序、搜索、首帧、峰值 RSS。
- `platform_matrix.md` / `platform_check.py` / `platform.json`：DPI 1/1.5/2、窄布局、浮动面板。

## 5. 截图

| 文件 | 说明 |
| --- | --- |
| `native_files_p0_inspector.png` | 单选：卡片层级、真实时长/大小、信息栏技术字段 + 标记控件 |
| `native_files_p0_multiselect.png` | 多选：文件数/总大小/合计时长/类型数聚合 |
| `native_files_p0_preview.png` | 原生预览浮层（适配窗口，真实解码结果） |
| `native_files_p0_preview_actual.png` | 原尺寸 1:1（可滚动） |
| `native_files_p0_list.png` | 详细列表：名称/类型/大小/修改时间/星级 |
| `native_files_p0_narrow.png` | 760px 窄布局：辅助栏自动收起，含旗标筛选 |
| `native_files_p0_batch_rename.png` | 批量改名预览：原名/新名/冲突（合成文件名夹具） |
| `native_files_p0_compare.png` | 并排比较：2 图同步缩放 70%（合成样张） |

重建命令：

```bash
PYTHONPATH=. .venv/bin/python docs/validation/files_native_p0/capture_ui.py
PYTHONPATH=. .venv/bin/python docs/validation/files_native_p0/capture_p2.py
```

## 5. 已知限制（不阻塞 P0 闭环）

- `/api/list` 无独立 `fps` 列：帧率仍从 `cached_info` 文本回退解析；要真实分数帧率需扫描器补探测列。
- 集合/堆叠仅提供浏览与成员读取；集合规则编辑、拖入集合仍由完整 Files 承担。F16 的堆叠展开按计划“有实际管理需求再做”，当前只提供旗标筛选。
- BX02–BX05 的迁移/重链/导出/插件/OCR 能力复用既有端点（`/api/meta/*`、`/api/export/run`、`/api/manager/plugins`、`/api/ocr/*`、`/api/similar`），只在触发条件出现时迁移为独立 service。
- 真机交互（真实鼠标拖拽、系统音量）建议在发布前人工复核；本文所有“通过”均指代码链路与自动化证据，不把截图当端到端。
