# GPU 后端清点（P0-01，自动生成）

> 由 `tools/inventory_gpu_backends.py` 生成于 2026-09-10T22:44:04.951943+00:00，commit `69ebfe1cc8db19057ab9b67c31b4e7407d07c2b1`。数字为当前 checkout 实测；行号以符号名为准。

## OpenGL/ModernGL 足迹

| 指标 | 值 |
|---|---:|
| 触碰 GL 的 .py 文件（src+external+native） | 53 |
| 其中行数 | 82096 |
| `.glo` 交接点 / 文件数 | 227 / 42 |
| `QOpenGLWidget` 引用 / 文件数 | 42 / 16 |
| `ctypes.CDLL(None)` 直连 GL | 5 处 |
| `from OpenGL import GL` | 17 处 / 7 文件 |

QOpenGLWidget 子类：

- `src/features/color/ui/gl_viewport.py:class GLCanvasViewport(QOpenGLWidget)`
- `src/features/composite/ui/composite_viewport.py:class CompositeViewer(QOpenGLWidget)`
- `src/features/edit/ui/program_viewer.py:class ProgramViewer(QOpenGLWidget)`
- `src/features/edit/ui/source_viewer.py:class SourceViewer(QOpenGLWidget)`
- `src/shared/ui/widgets/gpu_scope_widget.py:class GpuScopeWidget(QOpenGLWidget)`
二级子类：
- `src/shared/ui/widgets/cie_scope_widget.py:class CieGpuScopeWidget(GpuScopeWidget)`

`AA_ShareOpenGLContexts`：`src/main.py`、`src/features/shared/effects/effect_spec.py`

`.glo` 交接最多的文件（前 15）：

| 文件 | 处数 |
|---|---:|
| `src/features/composite/evaluators/filter_evaluator.py` | 31 |
| `src/features/edit/core/edit_gpu_preview_renderer.py` | 20 |
| `src/features/composite/render/gpu_resource_pool.py` | 18 |
| `src/features/color/core/async_renderer.py` | 17 |
| `src/features/composite/evaluators/color_evaluator.py` | 13 |
| `src/features/shared/effects/effect_pass_runner.py` | 10 |
| `src/features/composite/evaluators/base_evaluator.py` | 10 |
| `src/features/edit/core/timeline_clip_frame_provider.py` | 9 |
| `src/shared/gpu/gpu_scope_engine.py` | 9 |
| `src/features/composite/nodes/scene3d_node.py` | 8 |
| `src/features/composite/evaluators/channel_evaluator.py` | 8 |
| `src/features/composite/evaluators/keyer_evaluator.py` | 7 |
| `src/features/composite/nodes/output_node.py` | 6 |
| `src/features/composite/render/gl_utils.py` | 6 |
| `src/features/composite/evaluators/transform_evaluator.py` | 6 |

## Shader 语料

| 指标 | 值 |
|---|---:|
| `.glsl` 文件 / 行数 | 10 / 2826 |
| `#version` 指令总数（文件数） | 253（37） |
| `.program(` 编译点 | 90 |
| `usampler2D` | 45 |
| `imageStore` | 9 |
| composite compute 常量 | 10（被引用 7） |
| manager 已注册 compute | 7 |

`#version` 分布：`330`×228、`330 core`×1、`330 fragment shader`×1、`410 core`×10、`430`×13

## Metal 足迹（预期为 0）

- 无任何 Metal 符号/文件（Phase 1 是真正起点）

## 平台解码基线不对称

| 平台 | 文件 | 行数 |
|---|---:|---:|
| macOS `macos_vt/` | 10 | 6580 |
| Windows `windows_hw/` | 6 | 615 |
| Metal | 0 | 0 |

## 既有抽象（复用，勿重建）

- `GpuPresentFrame` → `src/shared/gpu/present_frame.py`
- `TextureLease` → `src/shared/gpu/present_frame.py`
- `OutputSlotPool` → `src/shared/gpu/present_frame.py`
- `EffectSpec` → `src/features/shared/effects/effect_spec.py`
- `EffectCapability` → `src/features/shared/effects/effect_capability.py`
- `DecoderBackend` → `external/file_explorer/plugins/video_pro/base_player.py`
- `playback_should_prefer_ffmpeg` → `src/shared/media/macos_vt/hardware_capability.py`
- `gpu_frame_gpu_api_literal` → `src/shared/media/gpu_frame.py`

## 关键文件行数

| 文件 | 行数 |
|---|---:|
| `src/features/composite/ui/composite_viewport.py` | 11492 |
| `src/features/composite/core/shaders.py` | 7338 |
| `src/features/color/core/async_renderer.py` | 6012 |
| `src/features/edit/core/edit_gpu_preview_renderer.py` | 4088 |
| `src/features/edit/ui/program_viewer.py` | 3279 |
| `src/shared/video/yuv_gpu_upload.py` | 3244 |
| `src/features/edit/ui/source_viewer.py` | 2314 |
| `src/features/color/core/shader_compiler.py` | 1083 |
| `src/shared/video/video_async_renderer.py` | 1047 |
| `src/features/color/core/shader_nodes.py` | 931 |
| `src/shared/gpu/gpu_scope_engine.py` | 663 |
| `src/features/composite/render/compute_shader_manager.py` | 584 |
| `src/features/composite/evaluators/histogram_manager.py` | 580 |
| `src/features/color/core/video_denoise_shaders.py` | 560 |
| `src/shared/ui/widgets/cie_scope_widget.py` | 556 |
| `src/shared/ui/widgets/gpu_scope_widget.py` | 510 |
| `src/shared/gpu/present_frame.py` | 429 |
| `src/features/color/ui/gl_viewport.py` | 191 |
| `src/shared/gpu/gl_backend.py` | 122 |
| `src/shared/gpu/gl_context_hub.py` | 121 |
