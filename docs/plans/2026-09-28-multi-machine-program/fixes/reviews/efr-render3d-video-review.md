# 复查报告：efr-render3d-video（E-rollup 机械清理批）

- 复查对象：提交 `a5612825d192b39d24667ae5635870abdc4cdb22`（模块行 render3d+video = `src/features/composite/render3d/` + `src/shared/video/`，137 条 process 级）
- 执行报告：`fixes/reports/efr-render3d-video.md`
- 复查日期：2026-10-01（GLM5.3 复查会话，只读复核，未运行 pytest，未做任何 git 写操作）

## 七项判定

| # | 检查项 | 判定 | 依据 |
|---|--------|------|------|
| 1 | 越界 | **PASS** | 39 个 .py 全部位于 render3d（22）/ video（17）两目录 + 1 个报告文件；tests/ 零触碰；REMEDIATION_PLAN.md 零触碰 |
| 2 | 未用导入删除 | **PASS** | 声称 67 名：53 名脚本核验 + 14 名多行手工核验，零代码残留；2 个脚本告警均为死函数体内惰性导入随死代码删除（见明细） |
| 3 | 死代码删除 | **PASS** | 声称 30 定义约 384 行：净删除 def/class 计数恰为 30，与报告表逐项一致；全部 28 个抽样名全仓 .py 零命中、非 .py 零命中（仅 audit/coverage 元数据） |
| 4 | 注解零行为 | **PASS** | 44 处 `-> None` 全声明级（diff 抽验 `__init__`/`_set_face_uniforms`/`_render_to_face` 等，参数列表原样） |
| 5 | 红线 | **PASS** | 零裸 except 新增；`QObject, pyqtSignal` 删除仅发生在 playback_state 的 TYPE_CHECKING 块（运行时零求值）；零 connect 触碰；GLSL/着色器文件零触碰 |
| 6 | py_compile | **PASS** | 全部 39 个改动源文件在当前 HEAD 下 `py_compile` 通过 |
| 7 | 综合结论 | **PASS** | — |

## 抽查明细

### 死代码（30 定义——按任务要求抽 4，实际全验 28 名）

全仓 `grep -rn --include='*.py' -w`（src/tests/scripts/tools/apps/benchmarks/plugins）逐名验证：

| 名 | py 命中 | 名 | py 命中 |
|----|--------|----|--------|
| **seek_master** | 0 | **seek_proxy** | 0 |
| **set_max_proxy_size** | 0 | **_show_first_frame_enhanced** | 0 |
| get_effective_proxy_size | 0 | should_drop_frame | 0 |
| get_lag | 0 | should_schedule_prefetch | 0 |
| schedule_prefetch | 0 | get_priority_frame | 0 |
| update_proxy_config | 0 | session_stats | 0 |
| get_command_history | 0 | has_pending_requests | 0 |
| reset_session_count | 0 | prefetch_frame_window | 0 |
| get_texture | 0 | get_all_results | 0 |
| _cleanup_video_renderer | 0 | set_proxy_enabled | 0 |
| _ensure_contiguous | 0 | mesh_local_aabb | 0 |
| coc_from_depth | 0 | point_attenuation | 0 |
| focal_from_fov / fov_from_focal | 0 | moderngl_filter_linear / _mipmap | 0 |
| has_ibl | 0 | selected_object_count | 0 |

非 .py 文件（json/qml/ui/yaml/toml/txt/html/js）同样零命中；docs/ 命中仅为 efr 报告自身与 `docs/audit/`、`docs/testcoverage/` 元数据（非代码）。

**二阶级联保守项**：`get_missing_prefetch_frames` 确认仍存活于 playback_clock.py:1362——报告"发现级联后保守不删"的决策属实。

### 未用导入（67 名）

脚本核验 53 名零代码残留。2 个告警澄清：
- material_texture_loader `moderngl`：模块级导入从未删除；告警源于被删死函数 `moderngl_filter_linear/_mipmap` 体内的函数级 `import moderngl`。存活代码的惰性导入（L219/231）完好。
- math_utils `np`：被删的是死函数 `point_attenuation` 体内的惰性导入；模块级 `import numpy as np`（L15）完好，L18 `np.eye` 正常。

多行/TYPE_CHECKING 删除手工核验（零残留）：viewport_overlays（QSize/TYPE_CHECKING/ViewportLayoutMode，随死块清空）、video_playback 13 名（safe_release_gpu_frame 实测文件内 0 命中；VideoPlaybackStats/FallbackEvent/create_fallback_event_from_context/has_recent_fallback/extract_codec_info/extract_pixel_format_info/extract_backend_info/FallbackDecision/is_fatal_fallback_reason/is_high_bitdepth_format/EffectPreviewQualityManager 全 0）、decode_worker MediaStatus→MediaSource(TYPE_CHECKING)、playback_state 5 名（os/QObject/pyqtSignal/SessionPlaybackState/SessionStats）、color_frame_provider_adapter FrameQuality、playback_clock np/CachePolicy/ProxyStrategy、session QueueEntry、frame_queue np。

保留项核实：frame_provider_service.py:683 与 :2028-2035 两处惰性导入防护块**逐行确认仍在**（ProxyStrategy/get_proxy_playback_priority 未用但按防护红线整行保留，与报告一致）。

### 注解与红线

- 44 处 `-> None`：diff 抽验 hdri `_set_face_uniforms/_render_to_face/_ensure_black_placeholders`、perf_stats/model_import_cache 等 `__init__`——参数列表逐字未动。
- playback_state 的 `-    from PyQt6.QtCore import QObject, pyqtSignal`（缩进 4 格）确认为 `if TYPE_CHECKING:` 块内删除——运行时恒不执行，信号机制零影响。
- video_playback.py L63 `from PyQt6.QtCore import QObject, pyqtSignal, QTimer, QThread, QMutex, Qt` 原样未动。

## 发现

- **Critical：0；Suggestion：0；Note：1**
- Note：被删的 seek_master/seek_proxy/set_max_proxy_size 等体内带 AUD-39306/AUD-39210 历史修复注释——删除正确（调用路径已在前序修复中移除，git 历史可溯），但建议中心知悉：若后续回归 AUD-39306 系问题，应先查这批方法是否需要复活而非重写。

## 结论

**PASS**。30 项死代码删除（约 384 行）取证扎实且全仓可复现零引用；67 名导入删除无一处代码残留，惰性导入防护与 TYPE_CHECKING 边界处理精确；44 处注解零行为；红线全绿；39 文件编译通过。
