# EFR Report: edit/core 模块机械清理（E-rollup）

- 日期：2026-10-01
- 分支：dev（工作树改动，未 add/commit）
- 范围：`src/features/edit/core/`（约 13.8 万行 / 134 个 .py 文件）
- 性质：纯机械清理，零行为变化。时间轴/播放引擎热路径仅做声明级改动（注解）。

## 一、分类统计

| 类别 | 扫描量 | 执行 | 明细 |
|------|--------|------|------|
| 1. 未使用 import | 254 候选 | **移除 62 个别名 / 45 文件** | 125 个 `from __future__` 按策略保留；其余全部经 AST + 全仓 grep 核实 |
| 2. 死代码 | 1163 个私有函数/方法 + 253 个模块级私有变量 | **删除 3 个死变量** | 私有符号全部存在至少 1 处全仓引用（含测试/字符串），0 个满足零引用删除条件 |
| 3. 类型注解 | 触及文件中 87 个缺注解 def | **补注解 13 处** | 6 处 `-> None`（脚本生成）+ 7 处手工保守注解；宁缺勿错 |
| 4. 命名收敛 | — | 0 | 模块内私有标识符未见可安全收敛项；公共 API/信号名一律未动 |
| 5. 小清理 | 重复字面量/冗余中间变量扫描 | 0 执行 | 识别并记录，见"剩余量" |
| 合计 | | **78 处改动，49 个文件，+60/-84 行** | |

## 二、抽样证据

### 1. 未使用 import（62 别名 / 45 文件）

流水线：AST 扫描（Name/Attribute 使用图）→ 分类（作用域/TYPE_CHECKING/try 惰性防护）→ 文本级字符串反射防护（别名在文件其余位置出现即跳过）→ 全仓 from-import 解析验证（含相对导入精确解析，排除重导出依赖）。

抽样（全仓零引用核实后删除）：

```
- generator_transaction_guard.py: from dataclasses import dataclass, field -> dataclass；typing 7 项 -> 4 项
- timeline_gpu_compositor.py: - from PyQt6.QtGui import QOpenGLContext；- PlaybackMode/SourceKind（保留 FrameHandle）
- project_serializer.py: - import uuid / typing.Any / dataclasses.asdict
- scene_cut/filters.py: - import math；frame_sampler.py: - import time；detector.py: - import uuid
- commands.py:14197 - from src.shared.project.color_stack import ColorStackData（模块中部死导入）
- timeline_preview_renderer.py:1963 - is_gpu_decoded_frame（多名字面量导入中仅删未用别名，保留 safe_release_gpu_frame）
```

保留并注明（非清理目标）：

- `from __future__ import annotations` ×125：任务明确允许使用，统一保留。
- 惰性导入防护 ×5：如 `audio_capabilities.py` `import av`、`lfilter` 位于 try/except ImportError 块内（可选依赖降级路径）。
- `timeline_preview_renderer.py:207` `if False:` 块（注释明示"Type hints for external modules (avoid circular imports)"）——视为守卫，保留 FrameQuality。
- `timeline_export.py:810/4376` try 守卫软依赖导入（有 fallback 绑定）。

### 2. 死代码（3 个死变量，全仓唯一出现即定义处）

```
- loudness_meter.py:82   _GATE_RELATIVE_LUFS = -10.0          # grep 全仓仅此 1 处
- take_sheet.py:32-36    _TAKE_METADATA_KEYS = (...)          # grep 全仓仅此 1 处
- title_templates.py:31  _STYLE_FIELDS = getattr(CaptionStyle, "STYLE_FIELDS", None)  # 全仓仅此 1 处
```

反面证据：1163 个 `_` 前缀函数/方法逐一做全仓词边界文本匹配（语料含 src/tests/scripts/docs/plugins 及配置文件，剔除 .venv/dist/__pycache__），全部存在外部引用，无一同态死代码——不删。

### 3. 类型注解（13 处，全部声明级）

脚本规则（仅零风险形态）：无返回/yield 的函数体补 `-> None`（6 处：commands.py ×4、edit_gpu_preview_renderer.py ×1、timeline_render_plan.py ×1）。

手工保守补注（7 处）：

```
audio_playback.py:641   __iter__(self) -> Iterator[float]                # TrackLevel.peak_l/r 均为 float
audio_playback.py:968   source_layout(self) -> Optional["ChannelLayout"] # 字符串前向引用，惰性导入不受影响
audio_playback.py:4440  set_track_routing 参数对齐 2080 行同名规范签名（Optional[str] / list）
bwf.py:105              _iter_chunks(data: bytes) -> Iterator[Tuple[bytes, int, int]]
captions/model.py:652   clip_style_base(clip: Any)
recording_manager.py:1144 _on_source_state_changed(state: "QAudio.State")  # 字符串注解，不影响信号派发
scene_cut/frame_sampler.py:379 __exit__ 参数补 Optional[type[BaseException]]/Optional[BaseException]/Optional[TracebackType]
```

未加注的 74 个 def：参数默认值为 None/可变对象/复杂类型，推断易错，按"宁缺勿错"跳过。

## 三、验证输出（全部通过）

1. `py_compile`：49 个改动文件逐一编译，0 失败。
2. 定向测试（11 个既有测试文件，均通过，无新增失败；未整目录跑，未触碰 test_p2_09_session_pool / test_track_sync_lock）：

```
tests/edit/test_bwf.py + test_playback_budget.py + test_audio_graph_snapshot.py  -> 42 passed (2.76s)
tests/edit/test_caption_data_model.py + test_scene_cut_cache.py                  -> 103 passed (1.27s)
tests/edit/test_scene_cut_cache.py + test_scene_cut_models.py + test_scene_cut_apply_ops.py -> 241 passed (1.31s)
tests/edit/test_loudness_meter.py + test_timeline_render_plan.py + test_audio_timestretch.py + test_adr_take_chain.py -> 126 passed (5.16s)
合计 463+ passed / 0 failed
```

3. `grep -rn "except:" src/features/edit/core/` → **0 命中**（清理前后均为 0；未修改任何 except 语义，全模块 `except Exception` 共 661 处按红线原样保留）。
4. 复扫流水线（清理后全量重跑）：未产生新的可删除项；唯一残余 SAFE 项即上文保留的 `if False:` 类型提示块。
5. diff 审计：`git diff` 全部删除行逐行核对，仅含 import 行、被注解替换的签名行、3 个死变量块。

## 四、改动文件清单（49 个，git 工作树未暂存）

```
src/features/edit/core/audio_graph_renderer.py      src/features/edit/core/audio_graph_snapshot.py
src/features/edit/core/audio_playback.py            src/features/edit/core/audio_timestretch.py
src/features/edit/core/audio_waveform.py            src/features/edit/core/automation_recorder.py
src/features/edit/core/bwf.py                       src/features/edit/core/captions/delivery.py
src/features/edit/core/captions/font_resolver.py    src/features/edit/core/captions/model.py
src/features/edit/core/captions/style_preset.py     src/features/edit/core/channel_layout.py
src/features/edit/core/commands.py                  src/features/edit/core/comp_clip_data.py
src/features/edit/core/edit_frame_coordinator.py    src/features/edit/core/edit_gpu_preview_renderer.py
src/features/edit/core/edit_playback_controller.py  src/features/edit/core/edit_project_io.py
src/features/edit/core/effect_instance.py           src/features/edit/core/generator_transaction_guard.py
src/features/edit/core/interchange.py               src/features/edit/core/loudness_meter.py
src/features/edit/core/project_serializer.py        src/features/edit/core/proxy_generator.py
src/features/edit/core/realtime_playback/audio_ring_buffer.py
src/features/edit/core/realtime_playback/avsync_controller.py
src/features/edit/core/realtime_playback/playback_budget.py
src/features/edit/core/realtime_playback/render_cache_adapter.py
src/features/edit/core/realtime_playback/source_resolver.py
src/features/edit/core/realtime_playback/timeline_decode_scheduler.py
src/features/edit/core/realtime_playback/timeline_gpu_compositor.py
src/features/edit/core/realtime_playback/types.py   src/features/edit/core/recording_manager.py
src/features/edit/core/render_cache_encoder.py      src/features/edit/core/render_cache_status.py
src/features/edit/core/scene_cut/cache.py           src/features/edit/core/scene_cut/detector.py
src/features/edit/core/scene_cut/filters.py         src/features/edit/core/scene_cut/frame_sampler.py
src/features/edit/core/scene_cut/metrics.py         src/features/edit/core/scene_cut/timeline_mapping.py
src/features/edit/core/scene_cut/worker.py          src/features/edit/core/scene_cut_controller.py
src/features/edit/core/take_sheet.py                src/features/edit/core/timeline_export.py
src/features/edit/core/timeline_occlusion.py        src/features/edit/core/timeline_preview_renderer.py
src/features/edit/core/timeline_render_plan.py      src/features/edit/core/title_templates.py
```

合计 +60 / -84 行（删除为主，符合清理性质）。

## 五、剩余量说明（有意不做的部分）

1. **39 个 import 候选**（别名在文件内有文本级出现）：多为字符串注解/文档字符串引用，需逐个人工判定，本轮保守跳过。
2. **TYPE_CHECKING 守卫内 text0 导入 8 个**：受守卫保护、运行时零成本，保留。
3. **类型注解 74 个 def**：集中在 edit_gpu_preview_renderer.py（33）/ timeline_export.py（22）/ commands.py（17），需逐个理解语义，机械加注风险大于收益。
4. **重复字面量提取**：已量化——`commands.py` `'Track not found: '` ×107、`'command_id'` ×208；`timeline_export.py` `'yuv422p10le'` ×32（且 20 处分布在其他文件，单文件提取破坏一致性）。站点数与"最小 diff"红线冲突，且多为 dict 键/元组键等异构上下文，需专项重构而非机械清理。
5. **冗余中间变量**：扫描命中 11 处均为 `x = f(x); return x` 重绑定惯用式（变量名有语义价值），内联属纯 churn，未执行。
6. **命名收敛**：未发现模块内部私有标识符存在可安全收敛项；未触碰任何公共 API/信号名。

## 红线自查

- 零行为变化：仅删死代码/死导入、加声明级注解；except 语义零修改。
- 无 git 写操作；分支保持 dev；REMEDIATION_PLAN.md 未触碰。
- 播放引擎热路径（realtime_playback/、edit_playback_controller.py、timeline_preview_renderer.py）仅删未用导入与补 `-> None`，未动任何逻辑。
