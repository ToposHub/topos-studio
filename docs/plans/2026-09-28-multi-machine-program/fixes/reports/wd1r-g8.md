# 修复报告 wd1r-g8 — 批 D 重派（Major ×7：AUD-3100~3104 / AUD-34001~34002）

- 范围：`src/app/controllers/page_controller.py`（AUD-3100~3104）、
  `src/app/integrations/file_explorer/adapter.py`（AUD-34001~34002）
- 证据：docs/audit/2026-09-28/batches/batch-031-app-controllers.md、
  docs/audit/2026-09-28/batches/batch-034-app-integrations-file_explorer.md
- 基线说明：本组为 Wave D1 死组重派。上轮 agent 的未验证改动已回退
  （page_controller.py / adapter.py / 两测试文件均为干净基线后开工）；
  page_controller 内 BUG-C3-01/02（传输条槽拆卸 / comp_clip_total_frames
  容错）已在基线中，本报告以当前代码为准。
- 测试：新建 `tests/app/test_fix_wd1g8_aud.py`（33 用例）；按锁定旧行为
  协议翻转 2 个白名单测试文件中的 3 个 pin 用例（详见各条目）。

## 结论总表

| 编号 | 结论 | 摘要 |
|------|------|------|
| AUD-3100 | **FIXED** | 补齐跨路径对称拆卸：新增 `_teardown_comp_clip_bar_slots()`（bar 槽 + 帧刷新槽连信号源一起存取），在 `_configure_composite_comp_clip_controls` 的全部早退判定之前执行；时间线路径接管/leave(enabled=False) 时也统一拆除 comp-clip 残留闭包，消除 `controller.seek` 与 `page.comp_seek` 双触发 |
| AUD-3101 | **FIXED** | 重试耗尽分支不再无差别自调度：实现了 exact-seek 接口的控制器一次 force_display 后止步；仅真 legacy（无 `seek_to_frame`）控制器保留重试且受硬上限 `_COLOR_STATIC_PREVIEW_MAX_RETRIES=20` 约束（计数递增）；彻底放弃时 warning + 撤防 clip-switch guard |
| AUD-3102 | **FIXED** | 探测移出 UI 线程：先以已知 tag 立即刷新监看 → 后台 daemon 线程探测 → `_ColorTagProbeSignals.probe_finished`（队列连接）回 UI 线程回填 `asset.metadata` 并二次刷新；带代际校验（过期结果丢弃）与同路径在途去重；非 QObject 宿主退回同步探测（保持旧语义） |
| AUD-3103 | **FIXED** | 抽出共享解析器 `_topmost_enabled_video_clip_at`（index 降序 = 最上层优先 + `is_enabled` 过滤），`_switch_to_color_following_edit_playhead` 与 `_find_clip_under_composite_playhead` 共用；按任务书明示约定翻转 playhead-sync 测试的重叠期望（first-hit → topmost） |
| AUD-3104 | **FIXED** | 异常分级：ImportError（旧组件）保持 debug；领域异常升为 warning + `exc_info=True`；`set_display_space` 被 viewer 拒绝时回退 `_apply_legacy_display_transfer`（transfer-only）并 warning 带堆栈，预览不中断 |
| AUD-34001 | **FIXED** | `read_port_config` 裸 `except:` → `except (OSError, json.JSONDecodeError)` + warning；非 dict 根拒绝；host 走回环白名单（`127.0.0.1`/`localhost`，挡 `0.0.0.0` 暴露面）；port 校验 `isinstance(int)` 且 1–65535（bool 显式排除），坏值回退默认并留日志 |
| AUD-34002 | **FIXED** | `FlaskWorker` 增 `_stop_requested` 标志（server 创建前/后各检查一次，早到 stop() 不再空转等 3s 超时）；`stop_server` 在 terminate 后追加 `wait(1000)`，仍未退出则保留引用并挂 `finished → 清引用 + deleteLater`，不再于线程运行中丢弃最后一个引用 |

全部 7/7 FIXED，0 INVALID，0 BLOCKED。

## 1. AUD-3100 [Major/bug/M] — FIXED

基线核实：BUG-C3-01 已拆掉时间线路径自身的 13 个闭包（存入
`_composite_timeline_bar_slots`，含 enabled=False 早退前拆卸），但审计指出的
**跨路径并存**仍在：`_configure_composite_comp_clip_controls` 的拆卸位于
image-color / viewer 缺失 / `not is_comp_clip_playback_active()` 三个早退判定
**之后**，comp-clip 激活进入后再进入时间线 backed Composite 时，13 个旧
comp-clip 闭包残留在同一 `video_control_bar` 上与新接的时间线槽并存
（两路径激活条件互斥：`is_comp_clip_playback_active()` ⟺ `_runtime_context
is None`，见 composite_page.py:4590-4594，故并存只能来自残留而非同帧双接）。
帧刷新槽 `_comp_clip_frame_slot` 同病（拆卸依赖当前 page 的信号且在早退后）。

修复（page_controller.py）：

1. 新增 `_teardown_comp_clip_bar_slots()`：`_comp_clip_bar_slots` 改存
   `(bar, slots)`（用存储的 bar 拆卸，viewer 变更后也能正确断开）；帧刷新
   槽改存 `(信号源, 槽)` 于 `_comp_clip_frame_wiring`，连信号源一起拆。
   拆卸用既有 while-disconnect 习语（清理历史累积副本）。
2. `_configure_composite_comp_clip_controls` 开头（一切早退之前）调用该
   拆卸；`_configure_composite_timeline_playback_controls` 在自身拆卸后亦
   调用（时间线接管与 leave(enabled=False) 时清掉 comp-clip 残留）。

测试（test_fix_wd1g8_aud.py 5 用例）：时间线接管后 bar 每信号恰 1 槽且
`comp_seek` 不再被触发；comp-clip 失活/image-color/leave 三条早退路径全部
清槽；重复接线幂等。

## 2. AUD-3101 [Major/bug/M] — FIXED

基线核实：审计原文的 50ms 主重试链（`retry_count+1 < 8`）与耗尽分支
100ms 同计数自调度（无限环）与报告一致（现行 2461-2488 行区域）。注释
声称 legacy-only 但未检查 `exact_seek` 是否存在。

修复：耗尽分支重构——

- exact-seek 可用：执行一次 `seek_to_frame(src, force_display=True)`
  （TypeError 时回退位置参数形式），成功即止（帧上传由渲染管线回填，
  上传清 guard 的既有机制不变）；失败（异常）→ warning + 撤防 guard，
  不再进入 10Hz seek 风暴。
- 真 legacy（`seek_to_frame` 不可调用）：保留 100ms 重试，但计数递增且受
  `_COLOR_STATIC_PREVIEW_MAX_RETRIES = 20` 硬上限（主链 8 + legacy 12 次 ≈
  1.2s @100ms），耗尽后 warning + `_disarm_color_clip_switch_guard`（视口
  不再永久黑屏）。

测试（test_fix_wd1g8_aud.py 2 用例 + coverage 翻转 1 用例）：legacy 链排空
到硬上限后停且 guard 被撤防（对排空次数设上限断言防回归）；exact-seek 抛
异常时不再调度任何重试且 guard 撤防。
锁定旧行为翻转：`test_page_controller_coverage.py::
test_static_preview_exhausted_retries_fall_back_to_exact_seek` 原断言
「exact-seek 控制器仍调度同计数重试」——正是审计认定的 bug 行为，按证据
文档明示语义翻转为「不再调度」（`recorder.calls == []`），并新增 legacy
上限用例补覆盖。

## 3. AUD-3102 [Major/risk/M] — FIXED

基线核实：`_update_color_monitor_for_asset` 在 UI 线程槽
（`_handle_enter_color_with_context` 链上）同步调用
`MediaSourceFactory.probe_video_metadata(path)`，首次未命中
`_PROBE_RESULT_CACHE` 时同步做容器探测（磁盘/网络 I/O），与审计一致。

修复（page_controller.py）：

1. `_update_color_monitor_for_asset`：先以已知 tag（可能全 None）立即刷新
   监看（页面切换不等待 I/O）；tag 缺失时调
   `_launch_color_tag_probe_for_host`（模块级入口，宿主可为非 QObject
   测试替身）。
2. 异步链：daemon 线程 `probe_worker` 执行探测 →
   `_ColorTagProbeSignals.probe_finished`（模块级 QObject，parent=宿主，
   AutoConnection 跨线程即队列投递）→ UI 线程
   `_on_color_tag_probe_finished` → 清在途集合 → **代际校验**（期间打开
   别的 clip 则丢弃）→ 写回 `asset.metadata`（仅非 None 值）→ 二次刷新监看。
   去重：`_color_tag_probes_inflight` 同路径在途不重复派发。宿主销毁后
   emit 抛 RuntimeError 被吞（daemon 收尾）。
3. 降级：宿主非 QObject（无线程边界信号可用，后台线程也无法安全回填
   canvas）→ `_probe_color_tags_sync_for_host` 同步探测回退，复用同一回填
   逻辑，保持旧可观察语义。

测试（test_fix_wd1g8_aud.py 4 用例 + coverage 翻转/新增 2 用例）：在途去重、
代际递增、非 QObject 宿主同步回退、完成回填+二次刷新、过期代际丢弃。
锁定旧行为翻转：`test_page_controller_coverage.py::
test_update_color_monitor_probes_and_backfills_metadata` 锁定「同步探测立即
回填」——证据文档明示「探测移至后台线程…首次未命中时先用占位/默认 tag
显示」，按该语义翻转为注入内联执行器（`_color_tag_probe_spawn`）驱动完整
异步链（同线程 emit = 直连，语义与队列路径一致），断言两次刷新（占位 →
探测值）与写回。非 QObject 同步回退保住了白名单外
`tests/color/test_color_monitor_status_bar.py`（SimpleNamespace 宿主 + 未在
白名单）的两个既有 pin 用例不动而全绿。
工程注意点：初次实现把 launch 放在占位刷新之前，内联执行时完成刷新会被
占位刷新覆盖（最终显示 None）——已改为「先刷新后 launch」，异步时序下同样
正确。

## 4. AUD-3103 [Major/bug/M] — FIXED

基线核实：`_switch_to_color_following_edit_playhead` 注释 "the first video
track covering the frame wins" 按 `video_tracks` 自然顺序首命中（且不过滤
`is_enabled`）；`_find_clip_under_composite_playhead` 为 index 降序 topmost
+ is_enabled 过滤——两条路径矛盾，与审计一致。

修复：抽出共享静态方法 `_topmost_enabled_video_clip_at(timeline, frame)`
（index 降序 = 最上层优先、disabled 视为 gap），两条路径共用。
`_find_clip_under_composite_playhead` 语义不变（coverage 既有 3 个 topmost
pin 用例全绿佐证）；`_switch_to_color_following_edit_playhead` 改走统一约定。

锁定旧行为翻转（任务书明示的约定决定）：`tests/app/
test_edit_to_color_playhead_sync.py::test_overlap_prefers_first_video_track_hit`
锁定旧 first-hit 约定（v1 胜）；按「重叠时偏好最顶层视频轨」约定翻转为
`test_overlap_prefers_topmost_video_track`（v2 胜），并新增
`test_overlap_skips_disabled_topmost_clip`（顶层 disabled → 次层胜）。
翻转理由：`_find_clip_under_composite_playhead` 的分层注释明示 "mirrors the
edit preview's layering"——edit 预览呈现给用户的就是最上层画面，跟随播放头
打开最上层 clip 与用户所见一致；旧注释引用的「Color playback advance /
multicam 首命中」路径（file_controller.py `_resolve_color_timeline_playing_clip`
等）不在本批白名单，其条带高亮语义保持原样，留待所属批次统一。

测试：test_fix_wd1g8_aud.py 4 用例（topmost、disabled-gap、乱序 track 也能
按 index 排序、两路径共用同一解析器返回同一对象）。

## 5. AUD-3104 [Major/bug/M] — FIXED

基线核实：`_apply_composite_preview_color_space` 尾部 `except Exception` →
`logging.debug`（无堆栈），try 块覆盖 descriptor 构建/HDR 探测/tonemap/
`set_display_space` 整条链，与审计一致。

修复（按审计建议逐项）：

1. `except ImportError` 单列 → 保持 debug（旧版组件缺失为预期回退）。
2. 其余 `except Exception` → `logger.warning(..., exc_info=True)`（领域
   失败可见、可排查）。
3. `set_display_space` 调用单独包裹：异常时 warning 带堆栈 + 回退
   `_apply_legacy_display_transfer(desc)`（原 transfer-only 路径抽为方法，
   旧版 viewer 分支复用），预览不中断。

测试（test_fix_wd1g8_aud.py 3 用例）：viewer 拒绝 set_display_space 时
warning + transfer-only 回退（PQ → 枚举 3）；descriptor 领域异常 warning 带
`exc_info`；ImportError 保持 debug（WARNING 级无记录）。

## 6. AUD-34001 [Major/bug/M] — FIXED

基线核实：`read_port_config` 裸 `except: pass`、host/port 无任何校验，与
审计一致（崩坏 JSON 静默、port 非 int 沿 AUD-34003 链形成静默失败、
`0.0.0.0` 暴露面）。

修复（adapter.py）：

1. 裸 `except:` → `except (OSError, json.JSONDecodeError)` + `logger.warning`
   （含配置路径与异常）。
2. 根类型校验：非 dict 根拒绝并 warning。
3. host 白名单 `_validated_host`：仅 `127.0.0.1`/`localhost` 放行，其余
   （含 `0.0.0.0`）回退 `127.0.0.1` + warning。
4. port 值域 `_validated_port`：`isinstance(int)`（bool 显式排除，bool 是
   int 子类）且 `1 <= port <= 65535`，坏值回退 `_default_port()` + warning。
5. 返回类型注解 `-> tuple[str, int]`；新增代码全部带类型注解。

（关联 Suggestion AUD-34003 `is_port_in_use` 裸 except 不在本批 7 条内，
未动，留待其所属批次。）

测试（test_fix_wd1g8_aud.py 9 用例）：合法配置直通、`0.0.0.0` 拒绝、
`"9000"`/0/70000/True/None 五类坏端口回退、坏 JSON/OSError/非 dict 根/
文件缺失回退，且坏 JSON 与 OSError 有 warning。

## 7. AUD-34002 [Major/risk/M] — FIXED

基线核实：`stop_server` 兜底 `terminate()` 后立即 `self.worker = None`；
`FlaskWorker.stop()` 以 `self.server is not None` 为前提（run() 前段窗口期
空转），与审计一致。

修复（adapter.py，按审计建议组合）：

1. `FlaskWorker` 增 `_stop_requested` 标志：`stop()` 无条件置位；
   `run()` 在进入 bootstrap 前、server 创建后进服务循环前各检查一次——
   早到的 stop() 让 run() 自行短路，不再只能等 `wait(3000)` 超时兜底
   （server 创建后的检查命中时 finally 仍幂等 stop 服务器）。
2. `stop_server`：terminate 后追加 `wait(1000)`；仍未退出（极端僵尸）→
   保留 `self.worker` 引用并挂 `finished → _forget_worker（清引用）+
   deleteLater`，绝不在线程运行中弃引用（消除 "QThread: Destroyed while
   thread is still running"）；正常退出路径行为不变（stop → wait → 清
   引用 + 日志），重复调用幂等照旧。
3. `FlaskWorker.__init__` 补类型注解（`app: Any, host: str, port: int`）。

测试（test_fix_wd1g8_aud.py 4 用例）：server 创建前 stop() 置位后 run()
短路（不创建服务器）；bootstrap 期间 stop() 到达则不进服务循环且 finally
配对 stop；僵尸 worker（wait 恒 False）terminate 后引用保留、finished 后
清引用；正常退出清引用 + 幂等。

## 验证

```
$ .venv/bin/python -m py_compile \
    src/app/controllers/page_controller.py \
    src/app/integrations/file_explorer/adapter.py \
    tests/app/test_edit_to_color_playhead_sync.py \
    tests/app/test_page_controller_coverage.py \
    tests/app/test_fix_wd1g8_aud.py
PY_COMPILE: OK (5/5)

$ .venv/bin/python -m pytest tests/app/test_page_controller_coverage.py \
    tests/app/test_edit_to_color_playhead_sync.py tests/app/test_fix_wd1g8_aud.py \
    -q -p no:cacheprovider --no-header
188 passed in 2.58s

# 邻接扩展
$ .venv/bin/python -m pytest tests/app -q -p no:cacheprovider --no-header
1129 passed in 26.26s          # 全量 tests/app

$ .venv/bin/python -m pytest tests/color/test_color_monitor_status_bar.py \
    tests/app/test_color_entry_playback_and_timeline_refresh.py \
    tests/app/test_color_timeline_clip_playback_mapping.py \
    tests/architecture/test_file_explorer_integration.py \
    -q -p no:cacheprovider --no-header
70 passed                      # 白名单外邻接（color monitor pin 未动全绿）

$ .venv/bin/python -m pytest tests/architecture -q -p no:cacheprovider --no-header
409 passed, 2 xfailed, 1 failed
# 唯一失败 TestAppLayerForbiddenImports 指向 src/app/controllers/
# ai_edit_controller.py:6301（本批未触碰的文件）；git stash 对照干净基线
# 同样失败 → 战役前既有违规（FIXLOG Wave 4 登记的 AUD-R2-009 存量债务），
# 与本批改动无关。
```

## 锁定旧行为翻转清单（共 3 例，均有证据/任务书背书）

| 测试 | 旧 pin | 新期望 | 依据 |
|------|--------|--------|------|
| test_page_controller_coverage.py::test_static_preview_exhausted_retries_fall_back_to_exact_seek | exact-seek 控制器耗尽后仍调度同计数 100ms 重试 | 不再调度（`recorder.calls == []`）；legacy 上限另立新用例 | AUD-3101 证据明示该环为 bug 及其正确语义 |
| test_page_controller_coverage.py::test_update_color_monitor_probes_and_backfills_metadata | 同步探测立即回填 | 占位刷新 + 异步完成刷新（内联执行器驱动），写回断言保留 | AUD-3102 证据明示「移至后台线程…先用占位/默认 tag」 |
| test_edit_to_color_playhead_sync.py::test_overlap_prefers_first_video_track_hit | 重叠取首个视频轨命中（v1） | topmost（v2）；新增 disabled-topmost 用例 | 任务书明示「重叠时偏好最顶层视频轨」约定及随测试更新的指示 |

## 遗留与说明

- AUD-34003~34009（Suggestion 级）不在本批 7 条内，未动。
- file_controller 的 `_resolve_color_timeline_playing_clip` /
  `_advance_color_timeline_active_clip` 仍为首命中约定（白名单外）；
  与本批统一后的 topmost 约定存在残差不一致的注记见 AUD-3103 节，建议
  归入其所属批次统一到共享解析器。
- 白名单外文件零改动；`git status` 中 composite_controller.py /
  wd1r-g4 等为并行组工作，与本报告无关。
