# EF-21 复查（GLM5.3）

- 日期：2026-10-01 ｜ 复查会话：GLM5.3（只读复核）
- 提交：`e74d7996d`（`[fix-E ef-21] 杂项散点 24条: 23 FIXED + 1 BLOCKED(AUD-12613 ...) (24)`）
- 白名单（e_flash_batches.json modules 键，19 个散点目录）：`src/features/composite`、`src/features/edit/adapters`、`src/shared/control_surfaces`、`src/shared/io`、`src/shared/node_help`、`src/app/menu`、`src/features/ai_workflow/core`、`src/features/ai_workflow/runtime`、`src/features/color`、`src/features/edit`、`src/features/scripting`、`src/features/teaching_animation`（含 motion/、presets/）、`src/products`、`src/shared`（含 i18n/、logging/、render/）
- 执行报告：`fixes/reports/ef-21.md`（结论 23 FIXED / 1 BLOCKED）

## §6 八项逐项判定

| # | 检查项 | 判定 | 依据 |
|---|--------|------|------|
| 1 | 越界 | **通过** | `git show --stat e74d7996d`：25 个文件 = 23 源 + 1 新测试（`tests/shared/test_ef21_aud.py`）+ 1 报告。23 个源文件逐一对照 JSON 19 个 modules 键全部命中（`src/features/edit/facade.py` 落 `src/features/edit`；`scene_cache.py` 落 `src/features/teaching_animation`；`app_restart.py` 落 `src/shared`）。**未触碰** `src/app/controllers/file_controller.py`（AUD-12613 根因所在，白名单外——BLOCKED 处置正确）。无 REMEDIATION_PLAN.md 改动。 |
| 2 | 真实修复（逐条） | **通过**（23 FIXED 全部 CONFIRMED；1 BLOCKED 核实成立，见下表及专项） | 逐 ID：grep 计划行 → 读报告结论 → HEAD 源码 file:line 独立核实 |
| 3 | GLSL/HDR 红线 | **通过** | 本批无 GLSL 着色器改动；`render_full_res_passes` 纯 Python 路由；diff 无 HDR 钳制、无 601/709 行（唯一 "601" 命中是 AUD-3600/3601 注释引用） |
| 4 | 并发 | **通过** | 关键异步/并发项均真实现：i18n 原子整体替换（AUD-33802）、log handler 先摘除后 close（AUD-34203）、scripting abort 持锁（AUD-25912）、scene_cache close/schedule 同临界区 + `_closed` 门卫（AUD-30502）、motion undo 令牌纪律（AUD-28116）、dispose 超时协作取消（AUD-23407）、tangent stop 残留线程保真（AUD-32801） |
| 5 | 资源 | **通过** | AUD-34203 句柄泄漏修复（detach→close，close 失败 warning）；AUD-6200 回退路径 warning 明示"调用线程将建独立 GL 上下文"（线程亲和性红线入 docstring）；AUD-1702 所有权契约（borrowed reference、禁止 caller release、GPU 输出限渲染线程/上下文） |
| 6 | 异常 | **通过** | 新增 except 8 处全部窄类型：`(ValueError, ZeroDivisionError)`（fcpxml 留痕降级）、`OverflowError`（转 ValueError 链栈）、`FileNotFoundError`/`json.JSONDecodeError`（i18n 文件读取）、`(OSError, ValueError)`（log close）、`OSError`（node_help）、`ImportError`（Qt 探测 pragma 降级）、1 处 `except Exception: # noqa: BLE001 — cancel 必须不阻断 teardown`（紧跟 `_LOG.warning(..., exc_info=True)` 留痕）。无裸 `except:`（grep 0 命中）。workflow_run_controller 中既有的 `except Exception: pass`（unsubscribe 等）为预存代码，不在本批 diff 新增行内 |
| 7 | 测试质量 | **通过** | `tests/shared/test_ef21_aud.py` 41 用例覆盖全部 24 条 ID（含 AUD-12613 的契约文档断言）。行为类修复为真断言（如 23209 副本改写不穿透冻结模板、33802 原子替换、34203 close 追踪 handler、6200 路由/告警）；文档契约类修复用 `inspect.getsource` 断言关键契约文本存在——对 doc-gap 类审计项是合理的回归锁定形式 |
| 8 | 复查产出 | 本文件 | 结论 **PASS**（含 1 条 BLOCKED 认定正确） |

### AUD-12613 BLOCKED 专项核验（复查指令指定）

- 根因确认：生产调用点 `src/app/controllers/file_controller.py` `_apply_interchange_import`（约 :7190-7208，本会话在 HEAD 读到 :7199 附近）在 UI 槽内同步调用 `build_project_from_import_result(...)`——`src/app/controllers` **不在** EF-21 白名单（本批 edit 侧仅 `src/features/edit/adapters`）。BLOCKED 而不动白名单外文件，处置符合规程。
- 契约告警核验：`src/features/edit/adapters/import_builder.py:137-148`（`collect_asset_rows` docstring："Thread affinity (AUD-12613)：… os.path.exists 阻塞磁盘探测 … 应改由 worker 线程调用"）与 :158-176（`build_project_from_import_result` docstring："对每个在线素材执行 os.path.exists + probe_metadata 逐素材阻塞探测 … 应由 worker 线程调用并把结果经信号/队列投递回 UI 线程"）——**两处 docstring 契约告警均确认在位**。
- 结论：BLOCKED 成立，白名单内能做的（契约显式化）已做。遗留：file_controller 侧迁移 worker 线程应立后续批次/台账项。

## 逐 ID 核实表（24/24）

| ID | 计划指控（摘要） | HEAD 证据 | 判定 |
|----|------------------|-----------|------|
| AUD-6200 | render_full_res_passes 无渲染线程路由 | `export_provider.py:126-232`：`render_thread` 参数 + `getattr(render_thread, "render_export_passes")` 路由（与 render_full_res 同契约）；无路由回退 `logger.warning("...calling thread...")`；docstring 写明线程亲和性红线 | CONFIRMED |
| AUD-9305 | 类文档声称 create_runtime 创建（死路径重复建页） | `runtime.py:15-24` docstring 更正为真实创建路径 `CompositeMainWindowAdapter.__init__`；`module.py:218-242` `create_runtime` 传 `existing_page=self._page` 复用 | CONFIRMED |
| AUD-12608 | _time_to_frames 静默 0 + int() 截断非 floor | `fcpxml_adapter.py:115-149`：小数秒走 `Fraction`（负值 floor 语义正确）；非法分母/解析失败 warning 留痕后按帧 0，返回值契约不变 | CONFIRMED |
| AUD-12613 | UI 线程同步阻塞 IO（根因在白名单外） | 见专项核验：BLOCKED 成立，白名单内 docstring 契约告警已写入 | BLOCKED（正确） |
| AUD-32801 | stop() 有界 join 超时后误清线程 → start 空跑双 worker | `hub_client.py:131-152`：join 超时 `logger.warning` + `return`（保留 `self._thread`，`is_running` 如实 True），正常退出才置 None | CONFIRMED |
| AUD-32803 | _pack_f32 OverflowError 违反 ValueError 契约 | `protocol.py:80-92`：`except OverflowError as exc: raise ValueError(f"float32 out of range: ...") from exc` | CONFIRMED |
| AUD-33910 | makedirs check-then-create TOCTOU | `file_io.py:301`：无条件 `os.makedirs(FileIO.CACHE_DIR, exist_ok=True)`（含注释） | CONFIRMED |
| AUD-33911 | docstring 声称 32 位浮点实为 HALF EXR | `file_io.py:55-72`：docstring 更正为 HALF(1) 16-bit（IMWRITE_EXR_TYPE=1），纯文档修正 | CONFIRMED |
| AUD-36401 | 语言文件读取失败不回退 en_US | `node_help/__init__.py:90-104`：`except OSError` → warning + `continue` 下一候选（含 en_US 回退），不再返回 None | CONFIRMED |
| AUD-36403 | has_node_help 读整个文档 | :127-142 起：纯 `os.path.isfile` 候选路径存在性探测，docstring 声明不打开不解码 | CONFIRMED |
| AUD-3603 | I18N 未使用 + "translated label" 失实 | `menu_builder.py:36-53`：docstring 更正（label 注册时已翻译，builder 不再翻译 action 标签；自动补建菜单标题仍走 I18N——AUD-3600/3601 已用上，指控自然消解） | CONFIRMED |
| AUD-23209 | instantiate 与冻结模板共享可变 dict | `ai_workflow/core/template_registry.py:135-176`：`default_param_overrides` 内层、节点 `params`/`ui`、`graph.ui.viewport` 全部 `copy.deepcopy` 边界拷贝 | CONFIRMED |
| AUD-23407 | dispose 超时既不取消也不处理线程 | `workflow_run_controller.py:467-497`：超时后 `runtime.cancel(reason="dispose_timeout")`（幂等）+ 二次 `wait(1000)` + 仍存活 `_LOG.warning`；不覆盖运行中 worker 引用 | CONFIRMED |
| AUD-24401 | 帧循环内死 import | `color/facade.py:240-243`：`TrackStatus` import 已删（注释留痕），status 经 getattr | CONFIRMED |
| AUD-19301 | 非 dict 外部数据原样透传 | `edit/facade.py:35-60`：非 dict/非 CompositeGraphData → warning + 返回 None（与"模块不可用"失败语义一致）；dict/已解析实例路径不变 | CONFIRMED |
| AUD-25912 | reset_abort/is_aborted 无锁读写 | `scripting/engine.py:162-182`：两方法均持 `self._mutex`（try/finally），与 request_abort/call_sync 同纪律 | CONFIRMED |
| AUD-30502 | close() 锁外 shutdown 与并发 schedule 窗口 | `scene_cache.py:59-79`：`_closed` 门卫（close 后 schedule 显式 RuntimeError）、shutdown+清理并入 `_lock` 临界区、close 幂等（docstring 论证无死锁） | CONFIRMED |
| AUD-28116 | 重复 apply 孤儿化先前动画 | `motion_commands.py:300-307/:374-.../:424-432` 三个命令 apply 发现令牌已置即 `raise RuntimeError`；三个 undo（:332-333/:399/:464）完成后清令牌（redo 可重放） | CONFIRMED |
| AUD-28323 | save_preset UI 线程同步渲染 GIF | `presets/library.py:49-69` `_warn_if_gui_thread`（GUI 线程 + render_preview=True → warning）；:253-292 `render_preview: bool = True` 参数 + Thread affinity 契约（UI 应传 False + PresetGifPreviewJob）。注：生产入口迁移在 ui/（白名单外）已如实披露为后续项 | CONFIRMED |
| AUD-21605 | include_locales 文档失实 | `packaging.py:53-71`：docstring 更正为"特性 ID，镜像 include_features，所有产品全量附带共享 locale 文件" | CONFIRMED |
| AUD-31903 | time.sleep(0.3) 不可靠错峰 | `app_restart.py:44-67`：固定延时删除（注释说明新进程自身端口顺延处理），spawn 后立即 quit；`import time` 一并移除（测试断言 source 无 `time.sleep`） | CONFIRMED |
| AUD-33802 | reload_locale 两步更新中间态窗口 | `i18n.py:299-337` 纯 helper `_merged_module_dir`（操作副本不触类状态）；:415-449 reload：局部构建完整合并结果 → :441-445 三属性一次性原子替换；失败路径（主文件损坏）返回 False 不污染活动词典 | CONFIRMED |
| AUD-34203 | handlers.clear() 不 close + 半清空窗口 | `log_manager.py:57-77`：`root.handlers = []` 原子整体替换（并发 emit 无半清空）→ 事后逐个 `close()`（失败 warning 留痕） | CONFIRMED |
| AUD-1702 | GPU 返回值无所有权/线程契约 | `render/source.py:42-63`：borrowed reference、禁止 caller release/缓存跨周期、GPU 输出限创建线程/GL 上下文、CPU numpy 可读但释放归 source | CONFIRMED |

## 红线发现

- 无 Critical、无 ISSUE。无裸 except；唯一新增宽 except（dispose cancel）有 `exc_info=True` warning 留痕；无 UI 线程 GL 调用（AUD-6200 恰是**强化**该红线：回退路径显式告警 + docstring 契约）；无 HDR 钳制、无色彩矩阵改动。
- 值得中心注意（非本批问题）：AUD-28323 的生产入口 `motion_graphic_editor_window._save_preset_triggered`（teaching_animation/ui/，白名单外）仍同步调用——与 AUD-12613 同属"根因在调用方"的遗留，建议进后续批次清单。

## 回退验证建议（抽 2 条）

1. **AUD-33910**
   - 文件：`src/shared/io/file_io.py:301`
   - 当前原文（old，逐字）：
     ```python
        os.makedirs(FileIO.CACHE_DIR, exist_ok=True)
     ```
   - 回退为（new，check-then-create 旧行为）：
     ```python
        if not os.path.exists(FileIO.CACHE_DIR):
            os.makedirs(FileIO.CACHE_DIR)
     ```
   - 应失败节点：`tests/shared/test_ef21_aud.py::test_aud33910_load_image_cache_dir_race_free`（fake_exists 使 TOCTOU 窗口复现 FileExistsError）
2. **AUD-23209**
   - 文件：`src/features/ai_workflow/core/template_registry.py:160-163`（instantiate 内 new_nodes 构造）
   - 当前原文（old，逐字）：
     ```python
                params={
                    **copy.deepcopy(n.params),
                    **merged_overrides.get(n.node_id, {}),
                },
                ui=copy.deepcopy(n.ui),
     ```
   - 回退为（new，共享引用旧行为）：
     ```python
                params={
                    **n.params,
                    **merged_overrides.get(n.node_id, {}),
                },
                ui=n.ui,
     ```
   - 应失败节点：`tests/shared/test_ef21_aud.py::test_aud23209_instantiate_breaks_shared_mutable_refs`（副本改写穿透冻结模板断言失败）；`test_aud23209_instantiate_applies_overrides_isolated` 同样暴露（`default_param_overrides` 内层仍共享时 nested 改写穿透）

## 最终结论

**PASS**。23/23 FIXED 真实、1 BLOCKED（AUD-12613）认定正确且白名单内契约告警在位、边界零越界、红线零违规。允许 `--mark`（commit 填 `e74d7996d`；AUD-12613 按 BLOCKED 结论标记并留 note，遗留 file_controller 迁移与 `_save_preset_triggered` 迁移建议进后续批次）。
