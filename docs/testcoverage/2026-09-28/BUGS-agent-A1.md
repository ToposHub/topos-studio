# 测试覆盖补齐 — Agent A1 疑似 Bug 报告

模块：`src/features/color/core/async_renderer.py`
日期：2026-09-28
来源：补测过程（tests/color/test_async_renderer_coverage.py 编写与验证）
约定：只记录，不修改 src。

---

## BUG-A1-01

- **编号**：BUG-A1-01
- **位置**：`src/features/color/core/async_renderer.py` `_do_bake_layer()`，
  `build_composition_shader(...)` 调用处（约 line 6418-6430，`request_id=render_request_id`）
- **描述**：`_do_bake_layer` 作用域内从未定义 `render_request_id`（全文件唯一的
  `render_request_id = params.get('render_request_id')` 赋值在 `_do_render_impl`，
  line 2319）。当栈前缀（`stack_prefix`）中存在**未冻结**（非 `is_baked`）的层时，
  该层会走到 `build_composition_shader(request_id=render_request_id, ...)`，
  Python 在求值实参时抛出 `NameError: name 'render_request_id' is not defined`。
  异常沿 `_handle_command` → `run()` 的 G-P0-11 try/except 被吞掉（仅打印
  traceback），`layer_baked_ready` 信号永不发射，整个 bake 静默失败。
- **证据**：
  1. 复现脚本（`AsyncRenderer.__new__` 桩 + `ctx=Mock()` + 一个非 baked 前缀层）
     稳定输出：`NameError: name 'render_request_id' is not defined`。
  2. `grep -n "render_request_id" async_renderer.py`：全部 6 处中只有
     `_do_render_impl` 内有赋值（line 2319），`_do_bake_layer` 内零赋值。
  3. 只有 `stack_prefix` 全部为已冻结层（走 `baked_tex is not None` 直通分支）
     时才不触发——正常“冻结最上层”的 bake 一定经过未冻结层，即正常路径必炸。
- **影响**：`BAKE_LAYER`（AI Baking 冻结层）功能在包含任一未冻结层的栈前缀上
  完全失效（无信号、无结果纹理上传），且无可见错误提示（只有控制台 traceback）。
- **置信度**：高（确定性复现，静态赋值缺失）。
- **修复建议**（未实施）：在 `_do_bake_layer` 开头补
  `render_request_id = data.get('render_request_id')`，或改用
  `request_id=data.get('render_request_id')`。

---

## BUG-A1-02（轻微 / 仅影响测试替身）

- **编号**：BUG-A1-02
- **位置**：`src/features/color/core/async_renderer.py` `_handle_command()` 的
  `RESET_VIDEO_DENOISE_HISTORY` 分支（line ~975，
  `pipeline = getattr(self, 'video_denoise_pipeline', None)`）
- **描述**：本文件对 `__new__` 构造的轻量测试桩有明确的防御惯例——`run()` 与
  `_do_render` 等处注释写明“__dict__.get 守卫：测试可经 __new__ 构造跳过
  __init__”，并用 `self.__dict__.get(...)` 读取可选属性。原因是未执行
  `QObject.__init__` 的实例上，缺失属性的普通 `getattr(self, x, default)` 会抛
  `RuntimeError`（PyQt6 抛 RuntimeError 而非 AttributeError，`getattr` 的默认值
  兜不住）。`_handle_command` 该分支用的是普通 `getattr`，与文件自身惯例不一致。
- **证据**：补测中以 `AsyncRenderer.__new__(AsyncRenderer)` 桩直接调用
  `_handle_command(RESET_VIDEO_DENOISE_HISTORY, None)` 抛
  `RuntimeError: super-class __init__() of type AsyncRenderer was never called`；
  改为显式设置 `video_denoise_pipeline=None` 后正常。
- **影响**：生产路径不受影响（真实实例总是跑过 `__init__`）；仅使测试替身在该
  分支上更难构造，属一致性/可测性瑕疵。
- **置信度**：高（现象确定）；严重度：低。
- **修复建议**（未实施）：改为
  `pipeline = self.__dict__.get('video_denoise_pipeline', None)` 与文件内既有
  惯例对齐。

---

## 附：本次补测覆盖数据（同口径对比）

- 口径：`pytest tests/color --cov=src.features.color.core.async_renderer`
  （3550 statements）
- 补测前（不含新文件）：20%（miss 2844）
- 补测后（含 `tests/color/test_async_renderer_coverage.py`）：33%（miss 2384）
- 新增覆盖语句：+460
