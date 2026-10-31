"""
Render command constants shared across video and color subsystems.

RenderCommand defines string constants used as command identifiers in the
rendering pipeline. Extracted here to avoid shared/ -> features/ dependencies.
"""

from __future__ import annotations


class RenderCommand:
    """String constants for render commands used by AsyncRenderer."""

    UPLOAD_IMAGE = "upload_image"
    UPLOAD_REFERENCE = "upload_reference"
    CLEAR_REFERENCE = "clear_reference"
    UPLOAD_VIDEO_FRAME = "upload_video_frame"
    UPLOAD_DECODED_VIDEO_FRAME = "upload_decoded_video_frame"
    UPLOAD_VIDEO_PLANES = "upload_video_planes"
    RENDER_VIDEO_FRAME = "render_video_frame"
    RENDER_VIDEO_GPU_FRAME = "render_video_gpu_frame"
    # COMPILE_PIPELINE 已随 P7-01 删除：build_shader 融合路径自始编译失败
    # （缺 BLUR_SHARPEN_SNIPPET，台账 0004a），零生产方；生产路径是
    # build_composition_shader（渲染时按需编译）。
    RENDER_FRAME = "render_frame"
    # Full-resolution export/apply render.  This is a critical command (never
    # lane-merged) because it owns a CPU readback result for one request.
    RENDER_FULL_RESOLUTION = "render_full_resolution"
    RENDER_COLOR_OUTPUT = "render_color_output"
    RENDER_SCOPE = "render_scope"  # deprecated CPU readback fallback (see docs/gpu_scope_implementation_plan_2026-08-05.md)
    RENDER_GPU_SCOPE = "render_gpu_scope"  # native GPU scope pipeline (no CPU readback)
    UPDATE_UNIFORMS = "update_uniforms"
    UPDATE_AUX_TEX = "update_aux_tex"
    CLEAR_AUX_TEX = "clear_aux_tex"
    REMOVE_LAYER = "remove_layer"
    BAKE_LAYER = "bake_layer"
    UPLOAD_BAKED_TEXTURE = "upload_baked_texture"
    CLEAR_BAKED_TEXTURE = "clear_baked_texture"
    UPLOAD_AI_PLUGIN_RESULT = "upload_ai_plugin_result"
    REMOVE_AI_PLUGIN_TEXTURE = "remove_ai_plugin_texture"
    RELEASE_VIDEO_TEXTURE_POOL = "release_video_texture_pool"
    # 视频降噪（时域+空域）参数更新 / 历史重置 / 暂停态邻帧窗口（docs/video_denoise_implementation_plan.md M4.2/M5）
    SET_VIDEO_DENOISE = "set_video_denoise"
    RESET_VIDEO_DENOISE_HISTORY = "reset_video_denoise_history"
    SET_VIDEO_DENOISE_WINDOW = "set_video_denoise_window"
