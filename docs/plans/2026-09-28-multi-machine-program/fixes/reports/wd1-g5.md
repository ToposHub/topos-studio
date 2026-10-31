# 修复报告 wd1-g5 — 批 D（Major ×14：AUD-2500~2509 / AUD-2600~2603）

- 范围：`src/app/controllers/deliver_controller.py`（AUD-2500~2509）、
  `src/app/controllers/dock_layout_controller.py`（AUD-2600~2603）
- 证据：docs/audit/2026-09-28/batches/batch-025-app-deliver-controller.md、
  docs/audit/2026-09-28/batches/batch-026-app-controllers.md
- 测试：新建 `tests/app/test_fix_wd1g5_aud.py`（23 用例）；
  按锁定旧行为协议翻转 `tests/app/test_dock_layout_controller_coverage.py`
  中 AUD-2600 的 1 个 pin 用例。

## 结论总表

| 编号 | 结论 | 摘要 |
|------|------|------|
| AUD-2500 | **FIXED** | fallback 收敛为 CLI/测试专用：UI 会话（存在 gl_viewport）直接拒绝在 UI 线程执行 GL（error + 返回 None，调用方回退 Color 输出）；保留路径显式 `ctx.release()`（无 Qt surface 被 makeCurrent，doneCurrent 不再需要） |
| AUD-2501 | **FIXED** | `perform_video_export` 入口防重入：既有 `_video_exporter.is_exporting` 时 QMessageBox.warning + return，不再覆盖引用/失联旧 QThread |
| AUD-2502 | **FIXED** | 异常边界收敛在 Qt 事件处理器：`export_file_as` 拦截 codec `ValueError` → logger.error + 状态栏 + QMessageBox.critical；`perform_video_export` 的抛错契约保留（AI host bridge 依赖它判定启动失败） |
| AUD-2503 | **INVALID** | 跨文件核实 `VideoExporter`：worker 经 Qt 信号 marshal 回主线程，用户回调全部在主线程执行，不存在跨线程 UI 操作 |
| AUD-2504 | **FIXED** | 新增 `_batch_extract_camera_wb`：优先复用 `load_image` 刚打开的 RAW 句柄（`_raw_lock` 内，消除二次 `rawpy.imread` 失败模式）；ImportError 与解码错误分相位捕获 + warning（后者带堆栈）；回退值显式命名为与 FileIO/引擎一致的「WB 未知」日光启发式 |
| AUD-2505 | **FIXED** | 新增 `_batch_load_image`：解码在 daemon 线程执行，UI 线程沿 `_batch_checkpoint` 同款泵事件习语等待（取消/暂停语义不变） |
| AUD-2506 | **FIXED** | `_batch_export_complete` 无条件回写快照值：None 还原为 None（file_path/图层/源图），集合字段还原为引擎初始默认 dict；不再残留最后一个批文件的状态 |
| AUD-2507 | **FIXED** | `.webp` 加入三条导出路径的 8bit 量化分支（panel/quick/batch，含 uint16→uint8）；批量保存失败由「返回 None 被标成 render returned no output」改为抛 `OSError`，真实原因进 item.error |
| AUD-2508 | **FIXED** | `_encode_composite_linear_to_srgb` 分相位收敛：ImportError → warning（预期环境差异）；descriptor 解析失败 → warning + 堆栈；仅 `transform_color` 意外失败 → **error + 完整堆栈**大声留痕后再降级 sRGB（导出不中断） |
| AUD-2509 | **FIXED** | `perform_batch_export` 入口防重入：`_batch_running` 生命周期标志（启动置位/完成复位），进行中（含暂停中）二次启动 warning + return，不再污染引擎状态快照/重置进度 |
| AUD-2600 | **FIXED** | 删除停靠分支对 floating_* 键的回写（此时 x()/y() 已是主窗口子部件坐标、尺寸非浮动尺寸，只会覆盖 file_controller 保存的正确几何）；浮动恢复路径补 `_ensure_window_on_screen` 钳制（屏幕外坐标拉回可见屏） |
| AUD-2601 | **INVALID** | 已由 Wave1 修复（BUG-B4-01 / AUD-03900 批次）：5 个 Edit 右侧 dock 已注册进 all_docks（现行代码 995-1004 行，含 [BUG-B4-01] 注释），coverage 测试 `test_created_docks_missing_from_all_docks_are_exactly_the_known_five` 已断言 `missing == set()` |
| AUD-2602 | **FIXED** | 三处裸 `except: pass`（原 2064/2074/3047）改为 `except (TypeError, ValueError)` + `logger.warning` 记录原始值；恢复分支同时拒绝零/负尺寸（位置负坐标合法不拒） |
| AUD-2603 | **INVALID** | 已由 Wave6-2 修复（BUG-B4-03）：`_enforce_page_dock_visibility` 已有 Deliver 分支（现行代码 2991-2998 行，含 [BUG-B4-03] 注释），initialize 兜底隐藏清单（1132/1179/1218/1465 行）也已含 dock_deliver，coverage 有 4 处锁定断言 |

## 1. AUD-2500 [Major/risk/M] — FIXED

现行代码（本批前）已把首选路径改为「CPU-only snapshot 投递给 CompositeRenderer
线程」（前次修复），但 fallback 仍在 UI 线程创建 GL context/纹理、`ctx` 从不
release、`makeCurrent` 后无 `doneCurrent`。

修复（deliver_controller.py:611-625, 711-725）：

1. **UI 会话拒绝执行**：`gl_viewport` 存在（= UI 会话）且 render thread 不可用
   时，`logger.error` + 返回 None——panel/quick/batch 三条调用路径均已有
   「回退 Color 输出」处理。函数 docstring 早已声明「无 Composite render
   thread 的 CLI/测试场景才保留独立 context fallback」，本次让代码真正执行
   该契约（`.clinerules`「UI 线程不调 GPU」）。
2. **保留路径资源归还**：CLI/测试 fallback（无 UI surface）补 `finally:
   ctx.release()`（moderngl 5.12 验证：`Context.release()` 存在，对 wrapped
   context 仅释放 ModernGL 包装不销毁底层 GL context）。该路径不再
   makeCurrent 任何 Qt surface（UI 路径已被拒绝），doneCurrent 因此不再需要。

偏差说明：审计建议「UI 路径直接报错并回退 Color 输出；若保留 fallback 用
ctx.release()」——两条建议均已实施。

测试：`TestAud2500CompositeFallback`（UI 拒绝 + makeCurrent 不被调用；
CLI 路径 ctx/texture release 各一次）。

## 2. AUD-2501 [Major/bug/M] — FIXED

`perform_video_export` 原直接 `self._video_exporter = VideoExporter(parent=self)`
覆盖引用。现入口处（:912-929）检查既有 exporter 的 `is_exporting`（该状态
`_cancel_video_export` 已在用；完成/失败回调经 `_cleanup_video_exporter`
清引用，空闲时为 None）：导出中 → QMessageBox.warning + return，引用不被
覆盖、旧 worker 不失联。

测试：`TestAud2501VideoReentry`（busy 拒绝且不创建第二 worker；idle 放行）。

## 3. AUD-2502 [Major/bug/M] — FIXED（异常边界与审计建议不同，理由如下）

审计建议把 `perform_video_export` 的 `raise ValueError` 改成弹窗 + return。
跨文件核实发现该异常是**编程调用方的既定契约**：
`src/app/controllers/ai_edit_host_bridge_impl.py:1298-1303` 以
`try/except Exception` 包裹 `start_export(...)`（即本方法）并据异常上报
「导出启动失败：…」。若改成静默 return，AI host bridge 会把 codec 拒绝误报为
「导出任务已启动」。

因此根因修复落在审计指出的真正缺陷点——UI 事件处理器无防护
（export_file_as:1203-1221）：`except ValueError` 拦截 → logger.error +
状态栏消息 + QMessageBox.critical（含 codec 名）。异常不再逃进 Qt 信号分发，
用户获得可见反馈；`perform_video_export` 契约与既有锁定用例
（`test_unknown_codec_raises_instead_of_silent_h264`）保持不变。

测试：`TestAud2502CodecErrorSurfaced`（export_file_as 不抛出、critical 弹窗
含 "bogus"、logger.error 留痕）。

## 4. AUD-2503 [Major/risk/M] — INVALID

审计自述「本文件无法确认 worker 是否已用信号 marshal 回主线程，需跨文件核实」。
核实 `src/shared/export/video_export_worker.py`：

- Worker（QThread 内，:97-186）只 emit Qt 信号（progress/finished/error）；
- `VideoExporter` 以 `parent=self`（主窗口）构造于主线程（deliver_controller
  :984），worker 信号 → `_on_worker_*` 槽为跨线程自动排队连接，槽内
  （:344-365，注释「内部槽：在主线程接收 worker 信号」）才调用用户回调；
- 因此 deliver_controller 传入的 on_progress/on_finished/on_error 闭包
  **恒在主线程执行**，`QMessageBox/processEvents/QWidget` 操作无跨线程问题。

结论：担忧的场景在本代码库不存在，条目按「已不存在（非缺陷）」处理。

## 5. AUD-2504 [Major/bug/M] — FIXED（回退值与审计建议不同，理由如下）

根因层面有两处问题：(a) 每个 RAW 文件被 `rawpy.imread` 打开两次——
`FileIO.load_image` 一次（成功，句柄保留在 `_current_raw_obj`）、WB 提取
又一次，第二次失败即落入 `except Exception: pass`；(b) 失败完全无日志。

修复（新增 `_batch_extract_camera_wb`，:1745-1800）：

1. **快路径**：`_raw_lock` 内复用 `load_image` 刚打开、路径匹配的 RAW 句柄
   （与 raw worker 的 `update_raw_decoding` 串行化）——加载已成功的文件不再
   有「二次打开失败」这一失败模式，且省一次完整解码；
2. **慢路径**：句柄不匹配/读取异常时退回一次性 `rawpy.imread`；
3. **错误分相位**：ImportError（rawpy 缺失）→ warning；解码/读取错误 →
   warning + `exc_info=True`；不再有任何静默吞噬；
4. **回退值**：保留 `[2.0, 1.0, 1.5, 1.0]` 并显式命名注释。

关于审计建议的 `[1,1,1,1]` 中性回退：该值是全库既定的「WB 未知」日光启发式
——`FileIO._get_normalized_camera_wb` 自身最后一级回退（file_io.py:420）、
`ImageEngine` 默认与交互路径提取失败回退（image_engine.py:380/502/819）用的
都是它。若仅批量路径改成中性值，同一文件「交互渲染失败回退」与「批量导出
失败回退」将产生不同颜色（违反审计自己在 AUD-2508 强调的导出/预览一致原则），
且中性 WB 对典型 RAW（G 通道占优）会引入更严重的绿偏。故根因按「消除失败
模式 + 全部留痕」修复，回退值与全库口径保持一致。

测试：`TestAud2504CameraWb`（快路径复用句柄；rawpy 缺失 warning；解码失败
warning 带堆栈 + 命名启发式）。既有用例
`test_raw_source_sets_raw_metadata_defaults`（断言 4 元素回退形态）继续通过。

## 6. AUD-2505 [Major/risk/M] — FIXED

新增 `_batch_load_image`（:1803-1846）：`FileIO.load_image`（磁盘 I/O +
rawpy/PSD/大 JPEG 全量解码）移入 daemon 线程，UI 线程以
`while worker.is_alive(): QApplication.processEvents(); time.sleep(0.01)`
等待——与 `_batch_checkpoint` 完全同款的既有习语（文件内 AUD-2516 对该习语
的忙等优化是 Suggestion 级、不在本批），绘制/取消/暂停事件持续流转。取消
语义不变：解码本身不可中断，完成后由下一个 checkpoint 生效（pending 重试）。
`_batch_export_single_file` 第 1 步改为调用它（:1842-1844）。

测试：`TestAud2505OffThreadLoad`（load 在非调用线程执行且结果透传；失败返回
三元 None + error 日志）。

## 7. AUD-2506 [Major/bug/M] — FIXED

`_batch_export_complete` 恢复块（:2026-2081）由 `is not None` 门控改为
**无条件回写**（快照字段在 `perform_batch_export` 必定先于批处理写入，None
即原始空值）：

- `current_file_path`：None → 显式还原 None（引擎初始态）；
- `layers`：None → `[]`；`active_layer_index`：None → 0；
- `current_raw_params`/`_last_applied_raw_params`：None → `{"enabled": False}`
  （引擎初始默认）；`current_raw_metadata`：None →
  `{"camera_wb": [2.0, 1.0, 1.5, 1.0]}`（引擎初始默认；渲染链用 `.get`
  读取，必须保持 dict 形态）；
- `img_full_original` 为 None 时显式清空 `img_full_original`/`img_source_full`
  （不再残留最后一个批文件的全分辨率源图），且不再对 None 调 `set_image`。

由此「导出前未打开图 → 完成后残留最后批文件 file_path/图层/raw 元数据 →
Ctrl+S 污染源文件 .topos」的数据覆盖路径被切断。

测试：`TestAud2506NoneSnapshotRestore`（残留态被清空、dict 形态保持、
set_image 不被调用）。既有 `test_engine_state_restored`（非 None 快照）
继续通过。

## 8. AUD-2507 [Major/bug/M] — FIXED

1. `.webp` 加入三条 dtype 转换路径的 8bit 分支：perform_panel_export（:224）、
   perform_quick_export（:279）、_batch_export_single_file（:1973，含
   float32→uint8 与 uint16→uint8 两档）。批量命名 fmt_map 的 "WebP": ".webp"
   由此获得正确的量化前置。
2. 保存失败误报（:1992-1996）：`FileIO.save_image` 返回 False 时抛
   `OSError("failed to save output image: <path>")`，由
   `_process_next_batch_file` 的既有 except 记入 `item.error`——IO/编码错误
   不再被伪装成 "render returned no output"（该文案仅保留给真正的渲染无输出，
   既有锁定用例 `test_failure_marks_failed_and_records_name` 不受影响）。

测试：`TestAud2507WebpAndSaveFailure`（float32/uint16 WebP 量化为 uint8；
保存失败抛带路径的 OSError）。

## 9. AUD-2508 [Major/bug/M] — FIXED

`_encode_composite_linear_to_srgb`（:86-135）的单一宽泛 except 拆为三相位：

- `import` 失败 → warning（环境缺依赖，预期路径）；
- `descriptor_from_config` 失败 → warning + `exc_info=True`（配置问题）；
- `transform_color` 失败（色彩管线 bug：LUT 加载失败/数组形状错误等）→
  **error + `exc_info=True`**，消息明确「导出颜色可能与预览不一致」，随后仍
  降级内置 sRGB OETF（导出不中断，调用方无崩溃风险）。

审计建议的「显式失败」会击穿 quick-export 路径（`_export_composite` 的
readback 编码不在 try 内），故采用其可接受的另一分支（warning/error +
exc_info 留痕）。

测试：`TestAud2508LoudDegradation`（transform 失败：error 记录带堆栈且仍输出
正确 sRGB 曲线；descriptor 失败：warning 且 transform 未被调用）。

## 10. AUD-2509 [Major/bug/M] — FIXED

- `perform_batch_export` 入口（:1395-1407）：`_batch_running` 为 True 时
  warning + return——不再覆盖引擎状态快照（避免把「已被第 N 个文件污染的
  中间态」当原始状态恢复）、不重置 `_batch_index`（避免两条 singleShot 链
  交叉）。暂停中的批量导出同样视为进行中（应先恢复/取消）。
- 生命周期：`perform_batch_export` 初始化状态时置 True（:1461-1462），
  `_batch_export_complete` 复位 False（:2085-2086）。

测试：`TestAud2509BatchReentry`（进行中拒绝且不动既有状态/不落盘队列；
标志启动置位、完成复位——含与 AUD-2506 用例的联合断言）。

## 11. AUD-2600 [Major/bug/M] — FIXED

`_on_dock_floating_changed`（dock_layout_controller.py:2088-2133）：

1. **删除停靠分支回写**：`topLevelChanged(False)` 时 dock 已被 reparent 进主
   窗口——x()/y() 是主窗口子部件坐标（恢复分支却按屏幕坐标解释）、
   width()/height() 已非浮动尺寸。旧代码把错误值写进 floating_* 键，覆盖
   file_controller 保存路径（file_controller.py:6827-6835 与 :6958-6969，
   均为「仍处浮动状态」时的正确几何）留存的正确值。浮动几何的持久化时机
   归位到该保存路径。
2. **恢复路径补钳制**：resize/move 后调用 `_ensure_window_on_screen(dock)`
   （复用主窗口恢复路径的尺寸+位置双钳制），换显示器/DPI 后指向不存在屏幕
   的保存坐标被拉回可见屏。

锁定旧行为协议：`tests/app/test_dock_layout_controller_coverage.py::
test_docking_transition_records_floating_geometry` 原 pin「停靠时写入
floating_* 键」，已翻转为断言「停靠不写 floating_* 键」（docstring 注明
AUD-2600 与翻转原因）。

测试：新文件 `TestAud2600FloatingGeometry`（停靠不写键；屏幕外坐标
(99999,99999) 被钳制回主屏、尺寸恢复正常）+ 翻转后的 coverage 用例。

## 12. AUD-2601 [Major/bug/M] — INVALID（前次批次已修复）

现行代码已修复且带回归测试，非本批重复处理对象：

- `dock_layout_controller.py:990-1004`：`dock_properties`、`dock_edit_effects`、
  `dock_caption_workspace`、`dock_adr_take_sheet`、`dock_multicam` 全部注册进
  `all_docks`，附 `[BUG-B4-01]` 注释（与审计描述的同型教训注释）；
- 修复来源：Wave1（`fixes/reports/w1-g4.md`，AUD-03900 批次含 BUG-B4-01）；
  `fixes/reports/w6-g2.md` 亦载明「BUG-B4-01 经核实已由 Wave1 修复」；
- 回归测试：`test_dock_layout_controller_coverage.py:337`
  `test_created_docks_missing_from_all_docks_are_exactly_the_known_five`
  断言 `missing == set()`（本批运行通过）。

## 13. AUD-2602 [Major/bug/M] — FIXED

三处裸 `except: pass`（原 2064/2074/3047，现 :2095-2113、2115-2123、
3101-3117）：

- 捕获收敛为 `except (TypeError, ValueError)`（解析失败预期路径；TypeError
  覆盖配置值为非字符串的损坏形态），`logger.warning` 记录原始值与 dock 名；
- 尺寸解析后增加 `width > 0 and height > 0` 校验（零/负尺寸拒绝应用，
  `_restore_docked_sizes` 同样拒绝向 `resizeDocks` 传 0 尺寸）；
- 位置负坐标合法（多显示器主屏左侧/上方的屏幕坐标为负），不拒绝；
  屏幕外问题由 AUD-2600 的 `_ensure_window_on_screen` 钳制兜底。

测试：`TestAud2602GeometryParseLogging`（坏值 warning 留痕且不 raise、
几何不变；`0x400` 非正尺寸拒绝；`_restore_docked_sizes` 对 "not-a-size" 与
"0x300" 的处理）。

## 14. AUD-2603 [Major/risk/M] — INVALID（前次批次已修复）

现行代码已修复且带回归测试：

- `_enforce_page_dock_visibility` 已有 Deliver 分支
  （dock_layout_controller.py:2991-2998，附 `[BUG-B4-03]` 注释）：
  `current_page != "Deliver"` 时强制隐藏 `dock_deliver`；
- 审计提到的兜底隐藏清单同样已补：initialize_page_layouts 的 3 段前置 +
  1 段收尾清单均已含 `dock_deliver`（:1132/:1179/:1218/:1465）；
- 修复来源：Wave6-2（`fixes/reports/w6-g2.md`：B4-02/03 明确记载
  「`_enforce_page_dock_visibility` 补 Deliver 分支」等）；
- 回归测试：`test_dock_layout_controller_coverage.py:615` 起 4 处锁定断言
  （BUG-B4-03 fixed），本批运行通过。

## 验证记录

```
$ .venv/bin/python -m py_compile src/app/controllers/deliver_controller.py \
    src/app/controllers/dock_layout_controller.py tests/app/test_fix_wd1g5_aud.py \
    tests/app/test_dock_layout_controller_coverage.py
（无输出，退出码 0）

$ .venv/bin/python -m pytest tests/app/test_deliver_controller_coverage.py \
    tests/app/test_dock_layout_controller_coverage.py tests/deliver \
    tests/app/test_fix_wd1g5_aud.py -q -p no:cacheprovider --no-header
397 passed in 12.55s
```

- 基线（改动前同命令）：374 passed。
- 397 = 374（基线）+ 23（新建 tests/app/test_fix_wd1g5_aud.py），无失败。
- 锁定旧行为协议：唯一冲突 pin 为 AUD-2600 的
  `test_docking_transition_records_floating_geometry`（原断言停靠时写入
  floating_* 键），已按协议翻转为断言修复后行为；其余 373 个既有用例原样
  通过（AUD-2502 保留 `perform_video_export` 的 ValueError 契约，两个
  codec pin 用例无需翻转）。
- 附加回归（非任务要求，防波及）：`tests/app/test_deliver_composite_frame.py`
  + `tests/app/test_deliver_batch_cancellation.py` → 18 passed。

## 改动文件

| 文件 | 改动 |
|------|------|
| `src/app/controllers/deliver_controller.py` | AUD-2500/2501/2502/2504/2505/2506/2507/2508/2509 根因修复（新增 `_batch_extract_camera_wb`、`_batch_load_image` 两个带类型注解的辅助方法；`import threading`） |
| `src/app/controllers/dock_layout_controller.py` | AUD-2600（停靠不回写 floating_*、恢复钳制）、AUD-2602（三处裸 except 收敛 + 非正尺寸拒绝） |
| `tests/app/test_dock_layout_controller_coverage.py` | 按协议翻转 AUD-2600 的 1 个 pin 用例 |
| `tests/app/test_fix_wd1g5_aud.py` | 新建，23 用例（覆盖 12 个 FIXED 条目的核心行为） |

## 残留与说明

- AUD-2505 仅覆盖计划条目点名的批量加载路径；审计顺带提及的单文件导出
  Lanczos resize（原 248/281/306）仍在 UI 线程（量级远小于 RAW 解码，
  且属 Suggestion AUD-2510/2511 的重构范围）。
- AUD-2504 回退值未按审计建议改为 `[1,1,1,1]`（与全库口径一致性优先，
  见 §5）；若项目后续决定全库统一改为中性回退，需同步 file_io/image_engine
  三处（超出本批白名单）。
- 工作树中其他 controllers 的未提交改动属并行批次（wd1-g3/wd1-g4），
  与本批无关、未触碰。
