# Edit Viewport 后端接入验收报告（VP-B01–VP-B12）

日期：2026-11-09。执行：后端批次（本仓库 `bugfix` 分支）。对应计划：
`docs/plans/2026-11-09-edit-viewport-backend-plan.md`（VP-B01–B12）。

- **基线 commit**：`283224a5`（上一批复审计划 R01–R10 收尾）。
- **本批终点 commit**：`4b9dd8b9`（共 12 个提交，见 §2）。
- **平台**：macOS 15.3 (darwin 24.3.0 x64)，Python 3.12.14，PyQt6 6.11.0 /
  Qt 6.11.2，offscreen (`QT_QPA_PLATFORM=offscreen`)。
- **GPU**：本机 CI 环境以 offscreen 运行；`tests/render/test_hdr_gpu_contract.py`
  在本机 2 passed（ModernGL standalone context）。真实 4K/HDR 输出质量属
  §5 证据边界。

## 1. 前提与工作区状态

前端容器（`edit_viewport.py`、`viewport_scrubber.py` 等）是**另一条并行
会话的未提交改动**，本批后端实现与之共存但从不提交它们。因此：

- 本批全部提交只包含后端模块 / 既有文件的定点修改 / 新测试；
- 凡需要"挂到前端容器"的接线，均以**单入口 + 鸭子类型信号**形式交付
  （见 §3 各条），前端合流时一行接入，不阻塞本批验收。

## 2. 提交清单

| 任务 | Commit | 交付物 |
|---|---|---|
| VP-B01 | `9c856ce4` + `b8cda235` | 仲裁器 + 协调器 + `is_playing` 公开态 |
| VP-B02 | `5aadf5ec` | present 配对回收 + 隐藏视图调度 + slot 计数 |
| VP-B10 | `332c5f9a` | Deliver 借还幂等边界 + 全屏父级 + 占用 dock 安全 |
| VP-B11 | `4173301d` | Source 关闭序列（token/顺序/资源释放）+ closeEvent 接线 |
| VP-B04 | `08086581` | Source 时间域（Rational fps/起始 TC/VFR/In-Out/状态存储） |
| VP-B05 | `eeccea7a` | Stop 原位 + Source Loop + 倒放（能力如实） + scrub 恢复 |
| VP-B06 | `bd3998e1` | Match Frame 权威映射 + 播放头策略 + 同素材候选 |
| VP-B03 | `a609db13` | 双视图预算政策 + 可序列化诊断 |
| VP-B09 | `61eb3419` | 版本化 viewport 偏好（校验回落 + 旧键迁移） |
| VP-B07 | `276149fd` | Viewer capability 注册表（单一事实源） |
| VP-B08 | `4b9dd8b9` | Source 拖拽：有版本 MIME + 三种流意图 + planner 路由 |
| VP-B12 | 本报告 + 计划文档记录 | 验收与证据边界 |

## 3. 已启用能力与接线点

| 能力 | 状态 | 接线点（前端合流时） |
|---|---|---|
| 两路播放互斥/权威快照 | 已落地（域件+适配层） | `install_viewport_playback_coordinator(viewport=…, source_widget=…, timeline_panel=…)`；Deliver 钩子 `edit_panels_borrowed/returned` 已在 `deliver_workspace` 发出 |
| 隐藏视图零周期解码/槽位闭环 | 已落地（widget 内建） | 单/双切换即生效（pane hide → hideEvent） |
| Deliver 往返 30 次/全屏借出/占用 dock | 已落地（deliver_workspace） | 现有 `_attach/_restore_edit_panels_to_deliver` 调用点自动获得 |
| Source 关闭序列 | 已落地（widget + closeEvent） | 无需前端动作 |
| 源时间域/In/Out 校验/状态恢复 | 已落地（域件） | `SourceTimeDomain` / `SourceMonitorStateStore` 供 source_viewer 换源恢复时调用 |
| Stop 原位 / Loop / 倒放视频 / scrub 恢复 | 已落地（controller+widget） | `stop_in_place()` / `set_loop()` / `play_reverse()`；菜单启用经 B07 注册表 |
| Match Frame | 已落地（域件 + 双击接线已在 timeline_view 生效） | F 键等宿主入口可直接调 `resolve_match_frame*` |
| 双视图预算/诊断 | 已落地（政策件） | B01 快照 + B02 计数喂给 `DualViewBudgetPolicy.decide` |
| 偏好版本化 | 已落地（解析件） | `parse_viewport_preferences` 替换 dock 层裸读两键 |
| 菜单 capability | 已落地（注册表） | 前端菜单按 `evaluate_viewer_capabilities` 的 supported/reason 启停 |
| Source 拖拽 A/V/A+V | 已落地（codec + timeline_view 消费端） | 前端 Source 画面/图标拖出时 `build_source_drag_payload(...)` + `QMimeData.setData(MIME_TYPE_SOURCE_CLIP, …)` |

## 4. 测试证据（offscreen，`.venv/bin/python -m pytest`）

新增套件（**全部先红后绿**：新模块以 ImportError 红，接线类以行为断言
红，再用实现转绿）：

```
tests/edit/test_viewport_playback_arbiter.py        16 passed
tests/edit/test_viewport_playback_coordinator.py     6 passed
tests/edit/test_source_viewer_public_state.py        1 passed
tests/edit/test_source_present_gate.py               9 passed
tests/edit/test_source_hidden_scheduling.py          8 passed
tests/edit/test_source_shutdown_lifecycle.py         3 passed
tests/edit/test_source_time_domain.py               17 passed
tests/edit/test_source_transport_adaptation.py      13 passed
tests/edit/test_match_frame.py                      13 passed
tests/edit/test_match_frame_view.py                  2 passed
tests/edit/test_viewport_budget_policy.py            6 passed
tests/edit/test_viewport_preferences.py              7 passed
tests/edit/test_viewer_capabilities.py               7 passed
tests/edit/test_source_drag_payload.py               7 passed
tests/edit/test_source_drag_drop.py                  7 passed
tests/deliver/test_viewport_deliver_roundtrip.py     8 passed
                                              合计 130 passed
```

计划 §VP-B12 指定的 focused 命令（工作区含并行前端 WIP 状态下）：

```
QT_QPA_PLATFORM=offscreen pytest tests/edit/test_workspace_presets.py \
  tests/edit/test_program_viewer_toolbar.py \
  tests/deliver/test_deliver_workspace_layout.py -q      → 30 passed
QT_QPA_PLATFORM=offscreen pytest tests/edit/test_source_viewer_playback_chain.py \
  tests/edit/test_source_viewer_playback_backpressure.py \
  tests/edit/test_source_viewer_scrub_preview.py -q      → 41 passed
pytest tests/media/test_timecode.py tests/media/test_f54_vfr_alignment.py -q
                                                         → 63 passed
pytest tests/render/test_hdr_gpu_contract.py -q          → 2 passed
tests/deliver 全目录                                      → 306 passed
```

全量 `tests/edit` 回归：见计划文档实施记录的最终数字（含对基线已知失
败集的逐项归属）。

## 5. 证据边界（诚实声明：offscreen CI 不能覆盖的项目）

1. **真实机性能（B03 门槛）**：4K60 双视图呈现率、UI p95、10 分钟内存
   稳定性需真机实测；本批交付的是政策与诊断件，**未宣称任何性能达标**。
   性能不达标时的降质政策落点已在 `DualViewBudgetPolicy` 预留。
2. **HDR/调色一致性（B12 表）**：HDR GPU 合约测试通过≠监看色彩验收；
   10-bit PQ/HLG 经新容器的端到端一致性需可视环境复核。
3. **手动操作手感（B01/B08）**：50 次监视器切换、拖拽阈值、Space 焦点
   分发的真机手感；域层语义已由测试钉死。
4. **显式不实现（计划允许的 disabled 项）**：Source Tape（素材集合/虚
   拟时间域未设计）、反向音频（`source_audio_reverse_supported=False`
   如实声明）、源 marker（工程 schema 未定）、monitor bypass（预览
   service 未接）、Source 外部监看——均在 B07 注册表带原因禁用。
5. **前端接线**：§3 表所列单入口在前端容器合流前无运行调用方；本批以
   假件测试钉死契约（鸭子信号名与前端现有实现一致）。
6. **真实素材 smoke**：`tests/test_videos` 的 2K 素材未进入本批用例
   （域级测试已覆盖语义）；真机 smoke（4K60/长 GOP/VFR/只音频/HDR/
   缺失素材 10 分钟稳定性）留待前端合流后整页联测。

## 6. 已知基线失败（与本批无关，沿用上一批归属）

`n03_grade`×5、`n03_mask`×3、`ui2_aud3`、`linked_ripple_revision_proxy`、
`timeline_disable_shortcut`（稳定），affordance×2、pointer×1（加载敏感
型像素比对）。若全量回归出现**上述集合之外**的新失败，归本批处理。
