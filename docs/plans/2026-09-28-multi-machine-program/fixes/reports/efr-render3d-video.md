# EFR 报告：src/features/composite/render3d + src/shared/video 机械清理（E-rollup 模块行）

日期：2026-10-02
范围：`src/features/composite/render3d/`（42 个模块文件，约 15,400 行，配额 69 条）+ `src/shared/video/`（31 个模块文件，约 27,800 行，配额 68 条）
原则：纯机械清理，零行为变化；GL/解码器调用序列与线程契约未动（死代码删除的均为全仓零引用定义，不改变任何在途调用链）；无 git 写操作；分支保持 dev。

## 分类统计

| 类别 | render3d | video | 合计 | 说明 |
|------|---------|-------|------|------|
| 1. 删除未使用 import | 20 个名字 / 16 文件 | 47 个名字 / 15 文件 | **67 个 / 31 文件** | AST 扫描 + 全仓 import 图核实（详见下文） |
| 2. 删除死代码 | 12 个定义 / 9 文件 | 18 个定义 / 9 文件 | **30 个定义（约 384 行）/ 14 文件** | 全仓文本零引用证明后删除（含证据表） |
| 3. 补类型注解 | 17 个 `-> None` | 27 个 `-> None` | **44 个** | 仅限 AST 可证明子集（见方法论） |
| 4. 命名收敛 | 0 | 0 | 0 | 未发现可证明安全的模块内部私有命名收敛点；公共 API/信号名一律未动 |
| 5. 小清理 | 0 | 0 | 0 | write-only 局部变量扫描零命中；重复字面量提取未采纳（见"未采纳项"） |
| 合计 | 22 文件，+26/−146 | 17 文件，+46/−340 | **39 文件，+72/−486** | `git diff --stat` 实测 |

裸 `except:`：两目录 `grep -rn "except:"` 零命中（既有即为零，无需修，也无新增）。

## 1. 未使用 import（67 个，全删）

核实方法（三层，与 efr-app-controllers 相同）：
1. AST 扫描（Name/Attribute/字符串常量 token 全覆盖；单行与多行 import 两次独立扫描）确认文件内零使用；
2. AST 全仓 import 图确认没有任何模块从上述模块 import 这些名字（含 re-export 与 `import a.b` 形式）；
3. 全仓文本确认无 `模块名.名字` 属性式访问、无 star-import 风险。

render3d（20 个）：
| 文件 | 删除的名字 |
|------|-----------|
| animation.py | `field`、`numpy as np` |
| aov.py | `field` |
| fields.py | `Optional` |
| ies_profile.py | `degrees` |
| material_model.py | `Optional` |
| material_texture_loader.py | `Tuple` |
| mesh_diagnostics.py | `Tuple` |
| mesh_vbo_pool.py | `Optional` |
| model_import_cache.py | `field` |
| node_icon_overlay.py | `field`、`LIGHT_TYPE_AMBIENT` |
| perf_stats.py | `field` |
| primitives.py | `Tuple` |
| scene_hierarchy.py | `numpy as np` |
| skeleton_renderer.py | `rest_pose_worlds` |
| viewport_overlays.py | `QSize`（含随之空置的 `if TYPE_CHECKING:` 块）、`TYPE_CHECKING`、`ViewportLayoutMode` |
| volume_grid.py | `Optional` |

video（47 个）：
| 文件 | 删除的名字 |
|------|-----------|
| color_frame_provider_adapter.py | `time`、`dataclass`、`field`、`numpy as np`（TYPE_CHECKING）、`FrameQuality` |
| decode_worker.py | `field`、`Callable`、`Union`、`MediaSource`（TYPE_CHECKING） |
| fallback_types.py | `time` |
| frame_provider_service.py | `Tuple`、`Callable`、`numpy as np`（TYPE_CHECKING） |
| frame_queue.py | `numpy as np`（TYPE_CHECKING） |
| pipeline_decision.py | `field` |
| playback_clock.py | `field`、`numpy as np`、`CachePolicy`、`ProxyStrategy` |
| playback_state.py | `os`、`QObject`、`pyqtSignal`（TYPE_CHECKING）、`PlaybackState as SessionPlaybackState`（TYPE_CHECKING）、`SessionStats`（TYPE_CHECKING；随死属性 `session_stats` 删除而失去唯一引用点） |
| session.py | `field`、`QueueEntry`（TYPE_CHECKING） |
| video_async_renderer.py | `os`、`QObject` |
| video_playback.py | `safe_release_gpu_frame`、`VideoPlaybackStats`、`FallbackEvent`、`create_fallback_event_from_context`、`has_recent_fallback`、`extract_codec_info`、`extract_pixel_format_info`、`extract_backend_info`、`FallbackDecision`、`is_fatal_fallback_reason`、`is_high_bitdepth_format`、`EffectPreviewQualityManager` |
| video_texture_pool.py | `field` |
| video_upload.py | `PixelFormatInfo` |
| yuv_gpu_self_test.py | `Optional`、`Dict`、`Any` |
| yuv_gpu_upload.py | `Union`、`field` |

保留（惰性导入防护，未动，注明如下）：
- `frame_provider_service.py:683` 函数内 `from ..media.proxy import ProxyConfig, ProxyStrategy, DEFAULT_PROXY_CONFIG` —— `ProxyStrategy` 在该作用域未使用，但属惰性导入防护（延迟 media.proxy 模块加载），整行保留；
- `frame_provider_service.py:2028-2035` 函数内 `from ..media.proxy import (..., get_proxy_playback_priority)` —— 同上，该名字在作用域内未使用，整块保留。

## 2. 死代码（30 个定义，全仓零引用证明）

删除前对每个名字执行全仓证据链：
1. AST Name/Attribute 引用索引（src/tests/scripts/tools/benchmarks 共 3,500 文件）零命中（定义行除外）；
2. 全仓文本（含字符串/getattr 动态调用面）按词边界 grep 零命中（定义行除外）；
3. `git grep -w` 全文件类型复查，仅命中 `docs/testcoverage/coverage.json` 与 `docs/audit/FINDINGS.json`（审计/覆盖率元数据，非代码引用）；
4. 排除 Qt 虚函数名、双下划线、Protocol/ABC 声明名（若协议声明过该名字会出现在第 1/2 步，实际为零）。

| 定义 | 位置（删除前行号） | 规模 |
|------|------|------|
| `mesh_local_aabb` | render3d/frustum.py:169 | 17 行模块函数 |
| `has_ibl` | render3d/hdri.py:486 | 3 行方法 |
| `moderngl_filter_linear` | render3d/material_texture_loader.py:223 | 3 行模块函数 |
| `moderngl_filter_mipmap` | render3d/material_texture_loader.py:228 | 4 行模块函数 |
| `point_attenuation` | render3d/math_utils.py:343 | 16 行模块函数 |
| `focal_from_fov` | render3d/math_utils.py:379 | 8 行模块函数 |
| `fov_from_focal` | render3d/math_utils.py:389 | 7 行模块函数 |
| `coc_from_depth` | render3d/math_utils.py:414 | 30 行模块函数 |
| `selected_object_count` | render3d/selection.py:169 | 2 行方法 |
| `has_pending_requests` | video/decode_worker.py:686 | 9 行方法 |
| `reset_session_count` | video/fallback_types.py:361 | 4 行方法 |
| `prefetch_frame_window` | video/frame_provider_service.py:1988 | 17 行方法 |
| `set_max_proxy_size` | video/frame_queue.py:363 | 15 行方法 |
| `get_effective_proxy_size` | video/frame_queue.py:379 | 3 行方法 |
| `seek_master` | video/playback_clock.py:770 | 22 行方法 |
| `seek_proxy` | video/playback_clock.py:793 | 22 行方法 |
| `update_proxy_config` | video/playback_clock.py:828 | 9 行方法 |
| `should_drop_frame` | video/playback_clock.py:1327 | 12 行方法 |
| `get_lag` | video/playback_clock.py:1340 | 9 行方法 |
| `should_schedule_prefetch` | video/playback_clock.py:1452 | 16 行方法 |
| `schedule_prefetch` | video/playback_clock.py:1493 | 8 行方法 |
| `get_priority_frame` | video/playback_clock.py:1506 | 14 行方法 |
| `session_stats`（@property） | video/playback_state.py:277 | 7 行属性 |
| `get_command_history` | video/playback_state.py:939 | 12 行方法 |
| `_cleanup_video_renderer` | video/video_playback.py:1124 | 8 行方法 |
| `set_proxy_enabled` | video/video_playback.py:2372 | 9 行方法 |
| `_show_first_frame_enhanced` | video/video_playback.py:3532 | 34 行方法 |
| `get_texture` | video/video_upload.py:599 | 11 行方法 |
| `get_all_results` | video/video_upload.py:775 | 3 行方法 |
| `_ensure_contiguous`（@staticmethod） | video/yuv_gpu_upload.py:901 | 4 行方法 |

注：其中若干方法体内含历史 AUD 修复注释（如 seek_master/seek_proxy 内 AUD-39306 游标同步、set_max_proxy_size 内 AUD-39201 释放契约）。这些定义本身已无任何调用方（修复后调用路径被移除所致），删除不改变任何在途调用序列；原修复仍保留在 git 历史与其余存活代码路径中。

级联死代码（本次未删，留待下轮）：`playback_clock.py get_missing_prefetch_frames`（约 9 行）——其仅有的两个调用方 `should_schedule_prefetch` / `schedule_prefetch` 本次按零引用证据删除后，它才变为零引用；属二阶变化，保守不删。

## 3. 类型注解（44 个 `-> None`）

方法论（AST 可证明子集，"拿不准就不加"）：
- 仅补 `-> None`：函数作用域内所有 `return` 均为裸 return/`return None`，或无 return；
- 排除：生成器（作用域含 yield）、stub（仅 docstring/pass/raise）、已有注解、@overload；
- **冲突防护**：同一方法名在仓库任何位置已存在非 None 返回注解时保守跳过（防破坏覆写约定）——54 处可证明，10 处因此跳过，另 4 处随死代码删除，最终应用 44 处；
- 多行签名经括号深度扫描定位插入点，冒号后仅允许注释；
- `bool` 返回本次两目录无可证明子集（0 处），参数注解一律未加；
- 注解为声明级改动，不求值，零运行时影响。

分布：video_playback 13、video_async_renderer 9、hdri 5、video_upload 3、mesh_vbo_pool 2、perf_stats 2、gizmo_overlay 1、gizmo_renderer 1、material_state 1、material_texture_loader 1、model_import_cache 1、node_icon_overlay 1、selection 1、denoise/frame_types 1、denoise/window_provider 1、video_texture_pool 1。

## 4/5. 未采纳项（记录决策）

- **命名收敛**：两目录未发现"同模块内同一概念两个私有名"这类可机械证明的收敛点；跨模块可见的标识符一律视为公共 API 不动。
- **write-only 死存储**：AST 扫描（赋值零读取、RHS 纯字面量/纯名字、无嵌套作用域捕获、排除 `_` 约定丢弃名）零命中。
- **重复字面量提取为常量**：需逐处语义审阅（同值不同义风险），超出机械清理边界，未采纳。
- **惰性导入中的未用名字**（frame_provider_service.py 两处，见上）：遵循"惰性导入防护保留"红线，未删。

## 验证（全过）

1. `py_compile`：39 个改动文件全部通过（`ALL_COMPILE_OK`）。
2. 定向既有测试（未整目录跑，未跑 tests/edit）：

```
tests/composite/test_render3d_stats.py + test_material3d_model.py   50 passed (0.87s)
tests/composite/test_skeleton_renderer.py + test_scene_hierarchy.py 33 passed (0.28s)
tests/composite/test_scene_payload.py                                38 passed (0.86s)
tests/video/test_video_async_renderer.py                              4 passed (0.26s)
tests/video/test_video_pipeline_fallback_types.py
  + test_video_pipeline_decision.py                                  71 passed (0.31s)
tests/video/test_video_texture_pool.py + test_yuv_gpu_upload.py      79 passed (3.71s)
tests/video/test_video_playback_coverage.py                         221 passed (1.28s)
```
合计 496 passed，0 failed，无新增失败。

3. 裸 except 检查：`grep -rn "except:" src/features/composite/render3d/ src/shared/video/` 零命中。

## 改动文件清单（39 个，工作树留改动，未 git add/commit）

render3d（22）：animation、aov、fields、frustum、gizmo_overlay、gizmo_renderer、hdri、ies_profile、material_model、material_state、material_texture_loader、math_utils、mesh_diagnostics、mesh_vbo_pool、model_import_cache、node_icon_overlay、perf_stats、scene_hierarchy、selection、skeleton_renderer、viewport_overlays、volume_grid（均 .py）

video（17）：color_frame_provider_adapter、decode_worker、denoise/frame_types、denoise/window_provider、fallback_types、frame_provider_service、frame_queue、pipeline_decision、playback_clock、playback_state、session、video_async_renderer、video_playback、video_texture_pool、video_upload、yuv_gpu_self_test、yuv_gpu_upload（均 .py）
