# EF-07 复查报告（GLM5.3）—— src/features/color/core 19 条

- 复查日期：2026-10-02 ｜ 复查会话：GLM5.3（只读复核，未改任何 src/tests/计划文件，未跑 pytest）
- 批提交：`efd7035456488426b062a669cea83cb705cf219a`（dev 分支）
- 白名单：`src/features/color/core/`（∪ tests/ ∪ fixes/reports/）
- 复查重点加码项（本批为 color/core）：色彩矩阵不可混用、不做无依据 HDR 钳制（>1.0 值域保留）、纹理/FBO 异常路径释放。

## 最终结论：**PASS**（附 2 条 Note，无 Critical、无 ISSUE）

---

## §6 逐项判定

### 1. 越界检查 —— 通过

`git show --stat efd703545` 共 18 个文件：

| 类别 | 文件 | 判定 |
|----|------|------|
| 源码 15 个 | async_renderer / cache_manager / color_output / color_session / denoise_engine / export_denoise_bridge / grade_keyframes / image_engine / layer / lut_exporter / opengl_render_backend / project_dependencies / the_decompiler / video_denoise_pipeline / video_temporal_denoise（均 .py，均在 src/features/color/core/） | 全部 ⊆ 白名单 |
| 测试 2 个 | tests/color/test_ef07_aud.py（新建，810 行 47 用例）；tests/color/test_fix_wd3g17_aud.py（既有更新，+10-5） | 允许；既有更新已在报告披露 |
| 报告 1 个 | fixes/reports/ef-07.md | 允许 |

- REMEDIATION_PLAN.md / e_flash_batches.json / FIXLOG.md 均未出现在 diff 中。

**既有测试更新（wd3g17）与披露一致性核实**：diff 显示 `TestAud0203MatteCacheDangling` 的失败注入手段从 `renderer.utility_shaders = {}`（依赖 AUD-0206 修复前的 KeyError）改为 `renderer._apply_matte_postprocess = Mock(side_effect=RuntimeError(...))`，断言由 `pytest.raises(KeyError)` 改为 `pytest.raises(RuntimeError)`。被验证的 AUD-0203 保证（生成中途失败时旧缓存条目所有权不被归还池）原样保留，注释说明了改动缘由。**该更新是 AUD-0206 修复的必然连带（旧注入手段恰好是被修复的缺陷），非测试迁就——披露成立，越界豁免通过。**

### 2. 真实修复（逐条独立核实，全部 CONFIRMED）

对每条 ID 均执行了：grep REMEDIATION_PLAN.md 指控 → 读 ef-07.md 结论 → 当前 HEAD 源码 file:line 独立核对。逐 ID 表见下节。

**重点条目独立判断：**

- **AUD-1003 决策记录：注释存在、理由经独立验证成立。**
  - 决策注释在 `video_temporal_denoise.py:273-291`（"AUD-1003 决策记录（保留这份全分辨率 f16 拷贝）"），含完整评估（下游只采样不写入、寿命契约相同）与后续实施路径。
  - 其理由可独立复核：`temporal_out` 表面键确实属于 RenderSurfaceCache 表面键契约——tests/color/test_fix_wd3g20_aud.py:232 的 causal 键集合断言包含 `"temporal_out"`；且 `_replay_window`（:460）以 `prefix + "temporal_out"` 保持窗口/因果表面图对称，单方面删除因果路径的该键属跨契约变更。
  - 审计原文（batch-010:63-79）的建议为"评估让 accum 直接渲染进 next_hist……**若下游确有写入冲突风险，则保留但记录该决策**"。执行方评估后选择了"保留 + 记录"分支——理由类别是"跨契约变更超最小改动边界"而非"写入冲突"，但两者都落在审计明文认可的处置集合内（审计置信度自评"中"，可去除性需下游确认——执行方正是做了该确认）。**判定：合法的审计认可分支，CONFIRMED（决策记录）。**
- **AUD-0206 的降级语义完整**：`_get_utility_shader`（async_renderer.py:996-1012，`.get()` + 每名一次 error 日志）+ 四类消费点降级全部核实——keyer×2（:3542-3545、:3642-3645，缺失→`ctx.clear(1.0,1.0,1.0,1.0)` 全白 matte）、morphology（:3724-3728 原样返回输入）、blur×2（:3812-3815、:3875-3878 跳过 pass）、matte_denoise（:3851-3852 跳过整块）。内联创建 shader（passthrough/raw_view/mask_combine 等）失败会经 AUD-0211 的 run() 初始化 try/except 走清理退出，消费点无需逐点守卫——分层正确。
- **AUD-0522 的修复过程回归被发现并修复**：锁引入导致 `copy.deepcopy`（Layer.copy_params_from）抛 "cannot pickle '_thread.lock'"，执行方以 `__getstate__/__setstate__`（grade_keyframes.py:178-186）把锁排除出序列化并在副本重建，报告如实披露。这是并发修复与既有 deepcopy 契约的正确调和。

### 3. 红线扫描（color/core 加码项）—— 通过

对 `git show efd703545` 全部新增行扫描：

- **裸 `except:`：0 处。`except Exception:` 新增 8 处，逐一核实留痕/恢复语义**：
  1. async_renderer.py VideoDenoisePipeline init（logger.exception + 降级 None）——原有宽捕获的移位收编；
  2. baked 纹理上传（logger.exception "Failed to upload baked texture" + 失败纹理 release）；
  3. baked release 二次防护（logger.debug exc_info=True）；
  4. AI 旧纹理释放防护（logger.debug exc_info）；
  5. AI 上传（logger.exception + release）；
  6. AI release 二次防护（logger.debug exc_info）；
  7. export_denoise_bridge fbo.read 失败（丢弃缓存重建重试一次；二次失败异常继续向上传播——恢复动作而非吞错，但该重试路径**无日志留痕**，见 Note A）；
  8. `_drop_out_fbo` 释放防护（logger.debug exc_info）。
  其余新增捕获均为具体类型（TypeError/ValueError/FileNotFoundError 等）+ warning 日志。
- **UI 线程 ModernGL：无新增**。AUD-0731 采用审计明文认可的"至少打日志"分支（`_warn_release_fn_off_thread`，opengl_render_backend.py:204-220：台账存在带 release_fn 记录且非渲染线程 → logger.error 列名），未引入硬守卫破坏 tests/shared/gpu 既有契约。
- **HDR 钳制：无新增 0-1 钳制**。diff 中 clip/clamp 相关命中均为原有代码或报告文本；AUD-0208 的形状校验是"拒绝上传"而非数值压缩；f16 纹理路径（dtype='f4'/'f2'）值域未动。
- **601/709 limited/full 矩阵：零触碰**（diff 无任何色彩矩阵代码）。
- **纹理/FBO 异常路径释放（本批加码重点）**：AUD-0208 两条上传路径均为"创建/write 包 try→失败 release→不登记"（baked :6903-6913；AI :6960-6973）；AUD-0211 run() 早退/初始化异常统一 `is_running=False + _cleanup()`（:496-545 两段核实）；AUD-0418 缓存 FBO 按纹理身份复用、read 失败丢弃重建、release() 先 drop FBO 再释放管线（:176-210）。释放顺序（FBO 先于其包裹的纹理）正确。

### 4. 并发 —— 通过

- AUD-0212：入口 `render_params = dict(data.get('render_params', {}))`（async_renderer.py:5497 附近），viewport_size 修正只写渲染线程私有副本。
- AUD-0301：`_state` 单元组整体替换（CPython 原子）+ `snapshot()` 一致快照 + 模块 docstring 线程契约。
- AUD-0319：`_state_lock` 锁内 check-then-act 与记账翻转，线程 start/join 在锁外（正确避免复刻 AUD-0310 重入死锁）。
- AUD-0522：轨道锁内快照求值/序列化；`__getstate__/__setstate__` 保持 deepcopy 契约。
- AUD-0612：入口 `dict(params)`、`user_wb` 存 `list(curr_wb)`、跨线程 Q_ARG 前再拷贝一次（image_engine.py:2044-2152 三处核实）。
- 无锁内重活、无线程取消检查点缺失新增。

### 5. 测试质量 —— 通过

tests/color/test_ef07_aud.py（810 行，47 用例，19 ID 全覆盖；全部 mock/`__new__` 构造，无真实 GL 上下文）。抽查断言质量：

- `TestAud0522`：真线程阻塞验证——持锁后另一线程调 `set_keyframe`/`value_at` 必须 `is_alive()`（"修复被回退"即死锁断言失败），附语义保持测试（外推/中点）。
- `TestAud0212`：断言调用方 dict `viewport_size == (0, 0)` 不变、渲染侧副本为 `(4, 4)`——精确体现"跨线程不改写 UI 侧对象"。
- `TestAud0415`：悬挂线程入台账 + 下次调用回收"迟到会话被丢弃"（`_FakeOrt` 挂起/放行控制）。
- `TestAud0206`：降级行为断言（`ctx.clear(1.0,1.0,1.0,1.0)` 全白、`_render_quad` 未被调、morphology/blur 原样返回输入且不分配中间纹理）+ 每名一次日志断言。
- `TestAud0208`：mock `ctx.texture/write` 抛错断言失败纹理被 release 且不登记。
- 弱断言评估：`TestAud1003` 为源码文本断言（决策记录存在 + 行为契约不变双断言）——决策记录类修复的合理手段。`test_snapshot_absent_means_revert`（TestAud0301 第三个）是 hasattr 存在性断言，较弱，但同 class 前两个用例已做元组值断言，整体覆盖充分。无可疑造假断言。

### 6. 回退验证建议（随机挑 2 条，供中心会话抽查回归有效性）

1. **AUD-0212**：文件 `src/features/color/core/async_renderer.py`，将 `_do_render_video_frame` 内（约 :5497）`render_params = dict(data.get('render_params', {}))` 改回 `render_params = data.get('render_params', {})`（去掉 `dict(...)` 拷贝；若 `data` 无该键则维持默认分支行为）→ 应失败节点：`tests/color/test_ef07_aud.py::TestAud0212::test_caller_render_params_not_mutated`（调用方 `viewport_size` 将被改写为 `(4, 4)`，断言 `== (0, 0)` 失败）。
2. **AUD-0301**：文件 `src/features/color/core/color_output.py`，恢复四字段分离赋值——`__init__` 中以 `self.texture_id: int | None = None; self.width = 0; self.height = 0; self.revision = 0` 替换单元组 `_state`，`update()` 改回四个属性分别赋值并删除 `snapshot()` 方法 → 应失败节点：`tests/color/test_ef07_aud.py::TestAud0301::test_update_snapshot_consistent`（`snapshot` 不存在直接 AttributeError）。

## 逐 ID 判定表（19/19）

| ID | 报告结论 | 复查判定 | 当前 HEAD 证据（file:line） |
|----|---------|---------|---------------------------|
| AUD-0206 | FIXED | CONFIRMED | async_renderer.py:896-905（load_prog 完整 error 日志）、996-1012（`_get_utility_shader`）；降级消费点 :3542/:3642（keyer 全白）、:3724（morphology 跳过）、:3812/:3875（blur 跳过）、:3851（denoise 跳过） |
| AUD-0208 | FIXED | CONFIRMED | async_renderer.py:6875-6885（baked 入口 HxWx3 校验，先于释放旧纹理）、6903-6913（baked 创建/write try + 失败 release）、6952-6970（AI 归一化 + 拒绝非 HxWx3）、6974-6987（AI write try + release） |
| AUD-0211 | FIXED | CONFIRMED | async_renderer.py:496-502（makeCurrent 失败早退：is_running=False + _cleanup + return）、507-545（初始化段统一 try/except → 清理退出） |
| AUD-0212 | FIXED | CONFIRMED | async_renderer.py:5493-5498（入口 `dict(...)` 浅拷贝，注释在），viewport_size 修正写私有副本 |
| AUD-0301 | FIXED | CONFIRMED | color_output.py:9-15（线程契约 docstring）、32-36（单元组 `_state`）、65-77（`snapshot()`）、86-89（update 整体替换） |
| AUD-0319 | FIXED | CONFIRMED | cache_manager.py:1750-1760（`_state_lock`）、1763-1766（is_running 走锁）、1768-1789（start 锁内 check-then-act + 线程 start 锁外）、1791-1799（stop 锁内翻转 + join 锁外） |
| AUD-0401 | FIXED | CONFIRMED | color_session.py:510-517（OrderedDict + `_max_history=64`）、536-543（move_to_end + popitem(last=False) LRU 淘汰） |
| AUD-0403 | FIXED | CONFIRMED | color_session.py:359-377（单行返回 + docstring 注明"ID 已在会话创建时解析"） |
| AUD-0415 | FIXED | CONFIRMED | denoise_engine.py:105-110（`_hung_loader_threads` 台账 + 锁）、433-452（先回收已结束悬挂线程 + info 留痕）、463-472（超时入台账 + warning 留痕） |
| AUD-0418 | FIXED | CONFIRMED | export_denoise_bridge.py:38-42（缓存字段）、153-162（`_get_or_create_out_fbo` 调用 + read 失败重建重试）、179-210（缓存/释放/`release()` 先 drop FBO） |
| AUD-0522 | FIXED | CONFIRMED | grade_keyframes.py:163-172（`_lock` 字段）、178-186（`__getstate__/__setstate__` 排除锁）、246-251（value_at 锁内快照）、283-286（to_dict 锁内快照） |
| AUD-0612 | FIXED | CONFIRMED | image_engine.py:2044-2053（入口 dict 拷贝）、2078-2081（`user_wb` 存 list）、2142-2148（Q_ARG 前再拷贝） |
| AUD-0704 | FIXED | CONFIRMED | layer.py:427-455（模块级 `_DEFAULT_QUALIFIER_PARAMS`）、546-547（init 共用）、628-631（reset 整组重建）、723-731（死代码 film_grain_params 分支删除 + docstring 移入"不复制"清单） |
| AUD-0714 | FIXED | CONFIRMED | lut_exporter.py:1064-1080（三回调 UniqueConnection + PyQt6 重复抛 TypeError 时 debug 留痕跳过） |
| AUD-0731 | FIXED | CONFIRMED | opengl_render_backend.py:108-126（release 调 `_warn_release_fn_off_thread`）、204-220（非渲染线程 + 带 release_fn 记录 → logger.error 列名；纯台账任意线程安全） |
| AUD-0820 | FIXED | CONFIRMED | project_dependencies.py:76-95（entries 非 dict 跳过 + warning）、147-167（size_bytes 转换 try (TypeError,ValueError) → 0 + warning） |
| AUD-0905 | FIXED | CONFIRMED | video_denoise_pipeline.py:81-91（params property 返回 `self._params.sanitized()` 只读快照 + docstring 契约） |
| AUD-0914 | FIXED | CONFIRMED | the_decompiler.py:421-460（非字符串/空→ValueError、不存在→FileNotFoundError、size<2→ValueError；docstring 性能契约"禁止 UI 线程同步调用"） |
| AUD-1003 | 决策记录 | CONFIRMED（审计认可分支，理由独立验证成立） | video_temporal_denoise.py:273-291（决策注释：temporal_out 表面键契约 + 后续路径）；契约证据 test_fix_wd3g20_aud.py:232、_replay_window:460 |

## 红线发现汇总

无 Critical。1 条 Note：

- **Note A**：export_denoise_bridge.py `_get_frame` 的 fbo.read 失败重试路径（:158-162 一带）`except Exception:` 后直接丢弃缓存重建重试，**无日志留痕**（虽有明确恢复动作、二次失败会向上传播，不构成吞错）。建议后续补一行 `logger.debug("Cached out FBO read failed; rebuilt once", exc_info=True)`，与同文件其余防护路径的留痕风格一致。

## 值得中心注意的事项（Note）

1. **AUD-1003 的优化收益被有意放弃**（4K 热路径每帧 ~0.5-1ms + ~66MB 读写），理由是表面键契约耦合。若后续做 RenderSurfaceCache 表面键契约的清理批次，该决策注释已写明实施路径（连同 test_fix_wd3g20_aud.py 键集合断言与 `_replay_window` 对称性一并调整），建议列入跟踪。
2. **AUD-0211 修复后 run() 的初始化失败不再让线程"带病空转"**，但 `_cleanup` 依赖各段自身守卫——执行方已核实 `_cleanup` 早退（ctx=None）安全且既有邻接测试（async_renderer_coverage 158 项等）全绿。中心统一跑测试时建议包含 tests/color/test_async_renderer_coverage.py 与 test_fix_wd3g17_aud.py（后者被本批连带更新过）。
3. AUD-0905 当前受 frozen dataclass 保护，本修复属契约固化（纵深防御）——报告已如实标注，不算虚报。
