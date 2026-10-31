# T00 · 三机边界冻结与基线 Manifest

> 状态：**已完成（2026-10-24）**。本页为 T 线开工的一页交接 manifest。
> 编制机：Mac（本机），T 线在独立分支 `codex/project-insights` 上推进，不动 `chore/color` 上的 M 线工作区状态。

## 1. 实测基线

| 项 | 值 |
|---|---|
| 开工 base commit | `dd1a28fdddb6ca970f3e2299d151c4741b6d2a97`（分支 `codex/project-insights` 自此切出；M 线 `chore/color` 已推进至 M09.2/M08.1b/M14.1/M24.1，晚于计划编制时观测的 `35a9ff904`——符合计划"开工须重新记录 base/head"） |
| 开工时 dirty 文件 | 无（工作树干净） |
| OS | macOS 15.3.1（Build 24D70），Intel x86_64 |
| CPU / 内存 | Genuine Intel / 64 GB |
| Python | 系统 3.9.6；项目 `.venv` 3.12.14（测试一律用 `.venv/bin/python -m pytest`） |
| Qt | PyQt6（offscreen 测试经 `tests/edit/conftest.py` 目录级注入 `QT_QPA_PLATFORM=offscreen`，`tests/edit/insights/` 继承，不需要新设） |
| GPU 依赖 | 无。T 线首批全部为纯 Python/DTO/offscreen-Qt 工作 |

## 2. 文件所有权（冻结）

T 线只新增/修改以下路径，其余仓库内容只读：

| 路径 | 权限 |
|---|---|
| `src/features/edit/insights/` | 新增，T 独占 |
| `tests/edit/insights/` | 新增，T 独占（含自有 conftest，不改 `tests/edit/conftest.py`） |
| `docs/insights/` | 新增，T 独占 |
| `docs/validation/upgrade_T/` | 新增，T 独占 |
| `docs/upgrade_plan_T_third_machine_2026-10-24.md` | 仅更新 T 任务状态/证据链接 |

不改：`edit/core/` 原模型与 commands/IO、`.topos` schema、renderer/decoder/音频/Deliver/QC/归档、`pyproject.toml`/打包基类、根 conftest/pytest.ini、共享 i18n 表、M/W 计划状态。发现这些范围的问题写交接记录（`docs/insights/handoff_*.md`），不顺手修。

## 3. G1/G2 责任（重申，不排队）

- **G1**（真实菜单/数据/导航/缩略图接线）：T 线交付协议（T01）、注入式 adapter/factory 与小补丁草案（T02）；共享文件（`src/features/edit/module.py`、`commands_facade.py`、`src/app/controllers/edit_controller.py`）由 Mac 集成者串行落地。G1 未合入前，T 线状态只到"API 已实现/组件已验"。
- **G2**（locales/templates 随包）：资源清单与期望由 T21 输出；`pyproject.toml` package-data / PyInstaller 收集由 Mac/Windows 打包集成者串行合入。

## 4. 已有能力复用清单（开工时复核属实，均不重复开发）

| 能力 | 入口（符号位置，开工时 HEAD `dd1a28fdd`） | T 线用法 |
|---|---|---|
| 两层工程状态 | `src/features/edit/ai_editing/edit_project_service.py:70` `ProjectSequences(project, sequences)`；宿主侧 `ai_edit_host_bridge_impl.py:338` `get_project_sequences()` | T01 capture 的唯一数据源形态；ref 在而 sequence 缺失 = `unloaded`，不造空时间线 |
| 快照脱敏纪律 | `ai_editing/snapshot_service.py`（构造式字段白名单 + `_redact_path` 只留 basename） | T01 DTO 白名单与路径脱敏照抄纪律 |
| 直接使用统计 | `core/asset_usage_index.py::build_asset_usage_index`（`timeline_start + timeline_duration` 半开语义） | T12 复用语义；间接依赖闭包（M20）不纳入 |
| 字幕子串搜索 | `edit/ui/caption_workspace.py:249` `find_in_clips`（`re.escape` + IGNORECASE，CJK 天然可用） | T10 采用同一匹配语义（字面子串、大小写不敏感） |
| 音频 cue CSV | `core/interchange.py::build_cue_sheet_from_sequence` | T11 引用不重写；注意其按 TC 字符串排序且 fps 归 float，T11 自身报表用精确 Rational |
| 时间码 | `shared/media/timecode.py::Timecode`（fps 为 `Rational`，DF/NDF；`auto_detect_type`） | 所有 TC 显示复用；不新增 float 时间码公式 |
| 有理数 | `shared/media/metadata.py::Rational`（num/den 自动约分） | fps 精确传递 |
| 缩略图 | `shared/media/thumbnail_provider.py` + `core/timeline_thumbnail_scheduler.py` | T13 经注入供应，不自建解码 |
| 菜单注册 | `edit/module.py:199` `register_menu_actions`（`MenuActionDescriptor(feature_id="edit")`） | G1 落点；`register_actions` 是占位，不接 |
| 编辑质量评估 | `ai_editing/editorial_quality.py:227` | T12 只解释计数，不复刻评分 |
| 合成工程 builder | `tests/edit/helpers/golden_project.py`（`build_minimal_golden_project` 等） | T04 fixture 参考其构造方式，但预期值独立手算 |

关键模型事实（fixture 构造依据，`core/timeline_v2.py`，行号为开工时）：
- record 侧字段是 `timeline_start/timeline_duration`（属性 `timeline_end`），source 侧是 `source_start/source_duration`；clip 无 `name` 字段，显示名解析自 `project.assets[asset_id].display_name`。
- `EditTrack.track_type ∈ {video, audio, subtitle}` 是字符串；字幕轨用 `subtitle_clips`（序列内 clip_id 全局唯一）；轨内 clip 不允许重叠（`__post_init__` 强制）。
- `TimelineSequence.fps` 归一为 `Rational`；`revision` 只在提交边界 bump。
- `ClipInstance.speed` 是 `SpeedParams | float`；`speed_points`/`curve_*` 域或 `reverse`/`freeze_source_frame` 存在时，源帧映射按未验收口径标 `unknown`（对应 M04 未收口）。
- `MediaAsset` 持有 `reel/scene/shot/take/keywords/rating/flag/color_label/comment` 日志字段（T10 搜索素材侧字段来源）。

## 5. 三机排程现状

- Mac/M 与 Windows/W 按 M/W 计划推进（本机 `chore/color` 分支的 M 提交即 M 线产出）；T 线分支独立，不阻塞、不被阻塞。
- T 线开工顺序：T00（本页）→ T01 → T04 → T10/T11/T12 →（T03、T20、T02-G1 草案并行排）→ T13/T14/T15 → T21/T22。

## 6. 用量记录

- Token/额度以账号控制台为准，本 manifest 不推算。本包（T00）Token 消耗：不可得（CLI 不回传），如实标注。
