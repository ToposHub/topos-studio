"""
Schema v1 Specification - .topos 子目录 JSON 结构定义。

定义 .topos ZIP 包中各子目录 JSON 文件的 schema v1 结构。
这些定义用于验证和生成 JSON 文件，确保数据一致性。

子目录结构：
project.topos
├── manifest.json           # 包元数据
├── project.json            # 工程核心索引
├── edit/
│   ├── media_pool.json     # 完整 MediaAsset 数据
│   ├── bins.json           # 完整 ProjectBin 数据
│   ├── timelines.json      # 完整 TimelineSequenceRef 数据
│   └── timeline_sequences.json  # TimelineSequence 数据
├── color/
│   ├── color_stacks.json   # 完整 ColorStackData 数据
│   └── looks.json          # Look 预设
├── composite/
│   ├── graphs.json         # 完整 CompositeGraphData 数据
│   └── viewer_state.json   # 合成查看器状态
├── deliver/
│   └── presets.json        # 导出预设
├── ui/
│   └── workspace_state.json  # UI 状态
└── cache_manifest.json     # 资产缓存清单

参考文档：
- docs/library_media_pool_project_architecture_plan_2026-05-19.md
"""

from __future__ import annotations

from typing import Any


# ================================================================
# manifest.json Schema
# ================================================================

MANIFEST_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_manifest_v1",
    "title": "Topos Manifest v1",
    "description": ".topos ZIP 包元数据，用于快速识别工程类型和版本",
    "type": "object",
    "required": ["format", "schema_version", "project_id", "project_mode"],
    "properties": {
        "format": {
            "type": "string",
            "const": "topos_project_archive",
            "description": "格式标识（固定值）"
        },
        "schema_version": {
            "type": "integer",
            "minimum": 1,
            "description": "Schema 版本号"
        },
        "created_by": {
            "type": "string",
            "default": "Topos Color",
            "description": "创建软件标识"
        },
        "project_id": {
            "type": "string",
            "format": "uuid",
            "description": "工程唯一 ID"
        },
        "project_mode": {
            "type": "string",
            "enum": ["single_asset", "edit_project", "composite_shot", "mixed_project"],
            "description": "工程使用形态"
        },
        "features": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["media_pool", "color", "composite", "edit", "deliver"]
            },
            "description": "功能特性列表"
        },
        "primary_asset_id": {
            "type": ["string", "null"],
            "description": "主要素材 ID（单素材工程）"
        },
        "active_timeline_id": {
            "type": ["string", "null"],
            "description": "当前活动时间线 ID"
        },
        "created_at": {
            "type": "number",
            "minimum": 0,
            "description": "创建时间戳（Unix time）"
        },
        "modified_at": {
            "type": "number",
            "minimum": 0,
            "description": "最后修改时间戳（Unix time）"
        }
    },
    "additionalProperties": False
}


# ================================================================
# project.json Schema
# ================================================================

PROJECT_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_project_v1",
    "title": "Topos Project v1",
    "description": "工程核心索引，引用各子目录数据",
    "type": "object",
    "required": ["project_id", "name", "project_mode"],
    "properties": {
        "project_id": {
            "type": "string",
            "description": "工程唯一 ID"
        },
        "name": {
            "type": "string",
            "minLength": 1,
            "description": "工程名称"
        },
        "project_mode": {
            "type": "string",
            "enum": ["single_asset", "edit_project", "composite_shot", "mixed_project"],
            "description": "工程使用形态"
        },
        "assets": {
            "type": "object",
            "description": "素材字典 {asset_id: MediaAssetRef}",
            "additionalProperties": {
                "$ref": "#/$defs/MediaAssetRef"
            }
        },
        "bins": {
            "type": "object",
            "description": "素材箱字典 {bin_id: ProjectBinRef}",
            "additionalProperties": {
                "$ref": "#/$defs/ProjectBinRef"
            }
        },
        "timelines": {
            "type": "object",
            "description": "时间线字典 {timeline_id: TimelineRef}",
            "additionalProperties": {
                "$ref": "#/$defs/TimelineRef"
            }
        },
        "color_stacks": {
            "type": "object",
            "description": "调色栈字典 {stack_id: ColorStackRef}",
            "additionalProperties": {
                "$ref": "#/$defs/ColorStackRef"
            }
        },
        "compositions": {
            "type": "object",
            "description": "合成图字典 {composition_id: CompositionRef}",
            "additionalProperties": {
                "$ref": "#/$defs/CompositionRef"
            }
        },
        "deliver_presets": {
            "type": "object",
            "description": "导出预设字典 {preset_id: DeliverPresetRef}",
            "additionalProperties": {
                "$ref": "#/$defs/DeliverPresetRef"
            }
        },
        "ui_state": {
            "type": "object",
            "description": "UI 状态字典"
        },
        "settings": {
            "type": "object",
            "description": "工程设置字典"
        },
        "primary_asset_id": {
            "type": ["string", "null"],
            "description": "主要素材 ID"
        },
        "primary_color_stack_id": {
            "type": ["string", "null"],
            "description": "主要调色栈 ID"
        },
        "active_timeline_id": {
            "type": ["string", "null"],
            "description": "活动时间线 ID"
        },
        "active_composition_id": {
            "type": ["string", "null"],
            "description": "活动合成图 ID"
        },
        "created_at": {
            "type": "number",
            "description": "创建时间戳"
        },
        "modified_at": {
            "type": "number",
            "description": "最后修改时间戳"
        }
    },
    "$defs": {
        "MediaAssetRef": {
            "type": "object",
            "required": ["asset_id", "path", "media_type", "display_name"],
            "properties": {
                "asset_id": {"type": "string"},
                "path": {"type": "string"},
                "path_relative": {"type": "string", "description": "相对路径（存储时）"},
                "path_original": {"type": "string", "description": "原始路径（用于重链接）"},
                "media_type": {
                    "type": "string",
                    "enum": ["image", "video", "audio", "image_sequence"]
                },
                "display_name": {"type": "string"},
                "is_offline": {"type": "boolean"}
            }
        },
        "ProjectBinRef": {
            "type": "object",
            "required": ["bin_id", "name"],
            "properties": {
                "bin_id": {"type": "string"},
                "name": {"type": "string"},
                "parent_id": {"type": ["string", "null"]},
                "asset_count": {"type": "integer"}
            }
        },
        "TimelineRef": {
            "type": "object",
            "required": ["timeline_id", "name"],
            "properties": {
                "timeline_id": {"type": "string"},
                "name": {"type": "string"},
                "fps": {"type": "number", "minimum": 0},
                "width": {"type": "integer", "minimum": 1},
                "height": {"type": "integer", "minimum": 1}
            }
        },
        "ColorStackRef": {
            "type": "object",
            "required": ["stack_id"],
            "properties": {
                "stack_id": {"type": "string"},
                "asset_id": {"type": ["string", "null"]},
                "name": {"type": "string"},
                "scope": {
                    "type": "string",
                    "enum": ["asset_default", "clip_local", "timeline_adjustment"]
                }
            }
        },
        "CompositionRef": {
            "type": "object",
            "required": ["composition_id"],
            "properties": {
                "composition_id": {"type": "string"},
                "name": {"type": "string"},
                "source_kind": {
                    "type": "string",
                    "enum": ["single_asset", "timeline_clip", "standalone"]
                },
                "asset_id": {"type": ["string", "null"]},
                "timeline_id": {"type": ["string", "null"]},
                "clip_id": {"type": ["string", "null"]}
            }
        },
        "DeliverPresetRef": {
            "type": "object",
            "required": ["preset_id"],
            "properties": {
                "preset_id": {"type": "string"},
                "name": {"type": "string"},
                "format_type": {"type": "string"},
                "resolution": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                    "maxItems": 2
                }
            }
        }
    },
    "additionalProperties": False
}


# ================================================================
# edit/media_pool.json Schema
# ================================================================

MEDIA_POOL_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_media_pool_v1",
    "title": "Media Pool v1",
    "description": "完整素材数据",
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "required": ["asset_id", "path", "media_type", "display_name"],
        "properties": {
            "asset_id": {"type": "string"},
            "path": {"type": "string"},
            "path_relative": {"type": "string"},
            "path_original": {"type": "string"},
            "media_type": {
                "type": "string",
                "enum": ["image", "video", "audio", "image_sequence"]
            },
            "display_name": {"type": "string"},
            "metadata": {
                "type": "object",
                "properties": {
                    "width": {"type": "integer"},
                    "height": {"type": "integer"},
                    "fps": {"type": "number"},
                    "duration_seconds": {"type": "number"},
                    "total_frames": {"type": "integer"},
                    "codec": {"type": "string"},
                    "pixel_format": {"type": "string"},
                    "bit_depth": {"type": "integer"},
                    "audio_info": {"type": "object"}
                }
            },
            "thumbnail_key": {"type": ["string", "null"]},
            "proxy_path": {"type": ["string", "null"]},
            "proxy_status": {
                "type": "string",
                "enum": ["none", "pending", "ready", "failed"]
            },
            "is_offline": {"type": "boolean"},
            "color_metadata": {"type": "object"},
            "created_at": {"type": "number"},
            "reel": {"type": "string"},
            "scene": {"type": "string"},
            "shot": {"type": "string"},
            "take": {"type": "string"}
        }
    }
}


# ================================================================
# edit/bins.json Schema
# ================================================================

BINS_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_bins_v1",
    "title": "Bins v1",
    "description": "完整素材箱数据",
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "required": ["bin_id", "name"],
        "properties": {
            "bin_id": {"type": "string"},
            "name": {"type": "string"},
            "parent_id": {"type": ["string", "null"]},
            "asset_ids": {
                "type": "array",
                "items": {"type": "string"}
            },
            "timeline_ids": {
                "type": "array",
                "items": {"type": "string"}
            }
        }
    }
}


# ================================================================
# edit/timelines.json Schema
# ================================================================

TIMELINES_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_timelines_v1",
    "title": "Timelines v1",
    "description": "完整时间线引用数据",
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "required": ["timeline_id", "name"],
        "properties": {
            "timeline_id": {"type": "string"},
            "name": {"type": "string"},
            "fps": {"type": "number", "minimum": 0},
            "width": {"type": "integer", "minimum": 1},
            "height": {"type": "integer", "minimum": 1},
            "sample_rate": {"type": "integer", "minimum": 1},
            "created_at": {"type": "number"}
        }
    }
}


# ================================================================
# edit/timeline_sequences.json Schema (P3)
# ================================================================

TIMELINE_SEQUENCES_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_timeline_sequences_v1",
    "title": "Timeline Sequences v1",
    "description": "TimelineSequence 数据（P3）",
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "required": ["timeline_id", "name"],
        "properties": {
            "timeline_id": {"type": "string"},
            "name": {"type": "string"},
            "fps": {"type": "number"},
            "tracks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "track_id": {"type": "string"},
                        "track_type": {
                            "type": "string",
                            "enum": ["video", "audio", "subtitle"]
                        },
                        "clips": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "clip_id": {"type": "string"},
                                    "asset_id": {"type": "string"},
                                    "start_frame": {"type": "integer"},
                                    "end_frame": {"type": "integer"},
                                    "track_start_frame": {"type": "integer"},
                                    "color_stack_id": {"type": ["string", "null"]},
                                    "composite_id": {"type": ["string", "null"]}
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}


# ================================================================
# color/color_stacks.json Schema
# ================================================================

COLOR_STACKS_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_color_stacks_v1",
    "title": "Color Stacks v1",
    "description": "完整调色栈数据",
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "required": ["stack_id"],
        "properties": {
            "stack_id": {"type": "string"},
            "asset_id": {"type": ["string", "null"]},
            "name": {"type": "string"},
            "grading_params": {
                "type": "object",
                "properties": {
                    "lift": {"type": "array", "items": {"type": "number"}, "minItems": 3},
                    "gamma": {"type": "array", "items": {"type": "number"}, "minItems": 3},
                    "gain": {"type": "array", "items": {"type": "number"}, "minItems": 3},
                    "exposure": {"type": "number"},
                    "temp": {"type": "number"},
                    "tint": {"type": "number"},
                    "contrast": {"type": "number"},
                    "shadow": {"type": "number"},
                    "highlights": {"type": "number"},
                    "saturation": {"type": "number"},
                    "vibrance": {"type": "number"},
                    "mid_detail": {"type": "number"},
                    "sharpness": {"type": "number"},
                    "hue_shift": {"type": "number"}
                }
            },
            "curves_data": {"type": "object"},
            "hsl_curve_data": {"type": "object"},
            "mixer_params": {"type": "object"},
            "soft_clip_params": {"type": "object"},
            "lut_path": {"type": ["string", "null"]},
            "lut_intensity": {"type": "number", "minimum": 0, "maximum": 1},
            "created_at": {"type": "number"},
            "modified_at": {"type": "number"},
            "trackings": {"type": "object"},
            "color_document": {
                "type": "object",
                "description": "完整 Color 文档（包含 layers, raw_params 等）",
                "properties": {
                    "layers": {"type": "array"},
                    "active_layer_index": {"type": "integer"},
                    "raw_params": {"type": "object"},
                    "source_bake": {"type": "object"},
                    "flip_state": {"type": "object"},
                    "film_grain": {"type": "object"},
                    "masks": {"type": "object"},
                    "plugin_stack": {"type": "array"},
                    "metadata": {"type": "object"}
                }
            }
        }
    }
}


# ================================================================
# composite/graphs.json Schema
# ================================================================

COMPOSITE_GRAPH_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_composite_graphs_v1",
    "title": "Composite Graphs v1",
    "description": "完整合成图数据（P1 序列化补齐，与 NodeEngine.to_dict() 格式一致）",
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "required": ["composite_id"],
        "properties": {
            # 核心字段
            "composite_id": {"type": "string", "description": "合成图唯一 ID"},
            "source_clip_id": {"type": "string", "description": "来源 clip ID"},
            "source_asset_id": {"type": "string", "description": "来源素材 ID"},
            "source_asset_path": {"type": "string", "description": "来源素材路径"},
            "name": {"type": "string", "description": "合成名称"},
            "width": {"type": "integer", "minimum": 1, "description": "输出宽度"},
            "height": {"type": "integer", "minimum": 1, "description": "输出高度"},
            # 节点（NodeEngine 格式：id, type, position 为数组）
            "nodes": {
                "type": "array",
                "description": "节点列表（NodeEngine 序列化格式）",
                "items": {
                    "type": "object",
                    "required": ["id", "type"],
                    "properties": {
                        "id": {"type": "string", "description": "节点 ID"},
                        "type": {"type": "string", "description": "节点类型"},
                        "position": {
                            "type": "array",
                            "description": "节点位置 [x, y]",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 2
                        },
                        "parameters": {"type": "object", "description": "节点参数"},
                        "disabled": {"type": "boolean", "description": "是否禁用"},
                        "expressions": {"type": "object", "description": "参数表达式 {param_name: expression}"},
                    }
                }
            },
            # 连接（NodeEngine 格式）
            "connections": {
                "type": "array",
                "description": "连接列表",
                "items": {
                    "type": "object",
                    "required": ["src_node_id", "src_port_name", "dst_node_id", "dst_port_name"],
                    "properties": {
                        "src_node_id": {"type": "string"},
                        "src_port_name": {"type": "string"},
                        "dst_node_id": {"type": "string"},
                        "dst_port_name": {"type": "string"},
                    }
                }
            },
            # P1 序列化补齐：新增字段
            "viewer_state": {
                "type": "object",
                "description": "合成查看器状态（zoom, pan, viewing_node_id）",
                "properties": {
                    "zoom": {"type": "number"},
                    "pan_x": {"type": "number"},
                    "pan_y": {"type": "number"},
                    "viewing_node_id": {"type": ["string", "null"]},
                    "show_grid": {"type": "boolean"},
                    "show_overlays": {"type": "boolean"},
                }
            },
            "viewport_state": {
                "type": "object",
                "description": "节点编辑器视口状态",
                "properties": {
                    "zoom": {"type": "number"},
                    "pan_x": {"type": "number"},
                    "pan_y": {"type": "number"},
                }
            },
            "backdrops": {
                "type": "object",
                "description": "背景框数据 {backdrop_id: {title, pos, size, color}}",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "pos": {"type": "array", "items": {"type": "number"}},
                        "size": {"type": "array", "items": {"type": "number"}},
                        "color": {"type": "string"},
                    }
                }
            },
            "sticky_notes": {
                "type": "object",
                "description": "便签数据 {note_id: {text, pos}}",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "pos": {"type": "array", "items": {"type": "number"}},
                    }
                }
            },
            "groups": {
                "type": "object",
                "description": "节点组数据 {group_id: [node_ids]}",
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            },
            # 时间戳
            "created_at": {"type": "number", "description": "创建时间戳"},
            "modified_at": {"type": "number", "description": "最后修改时间戳"},
        }
    }
}


# ================================================================
# composite/viewer_state.json Schema
# ================================================================

COMPOSITE_VIEWER_STATE_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_composite_viewer_state_v1",
    "title": "Composite Viewer State v1",
    "description": "合成查看器状态",
    "type": "object",
    "properties": {
        "zoom": {"type": "number"},
        "pan_x": {"type": "number"},
        "pan_y": {"type": "number"},
        "show_grid": {"type": "boolean"},
        "show_overlays": {"type": "boolean"},
        "selected_node_id": {"type": ["string", "null"]}
    }
}


# ================================================================
# deliver/presets.json Schema
# ================================================================

DELIVER_PRESETS_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_deliver_presets_v1",
    "title": "Deliver Presets v1",
    "description": "导出预设",
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "required": ["preset_id", "name"],
        "properties": {
            "preset_id": {"type": "string"},
            "name": {"type": "string"},
            "format_type": {
                "type": "string",
                "enum": ["h264", "h265", "prores", "dnxhr", "exr", "tiff", "png", "jpg"]
            },
            "resolution": {
                "type": "array",
                "items": {"type": "integer"},
                "minItems": 2,
                "maxItems": 2
            },
            "fps": {"type": "number"},
            "codec_settings": {"type": "object"},
            "color_settings": {"type": "object"},
            "audio_settings": {"type": "object"},
            "output_path_template": {"type": "string"},
            "metadata": {"type": "object"}
        }
    }
}


# ================================================================
# ui/workspace_state.json Schema
# ================================================================

WORKSPACE_STATE_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_workspace_state_v1",
    "title": "Workspace State v1",
    "description": "UI 工作区状态",
    "type": "object",
    "properties": {
        "active_page": {
            "type": "string",
            "enum": ["color", "edit", "composite", "deliver", "library"]
        },
        "color_page_state": {
            "type": "object",
            "properties": {
                "primary_viewer_zoom": {"type": "number"},
                "primary_viewer_pan": {"type": "object"},
                "reference_viewer_state": {"type": "object"},
                "panel_layout": {"type": "object"}
            }
        },
        "edit_page_state": {
            "type": "object",
            "properties": {
                "timeline_zoom": {"type": "number"},
                "timeline_scroll_position": {"type": "number"},
                "selected_clip_ids": {"type": "array", "items": {"type": "string"}}
            }
        },
        "composite_page_state": {
            "type": "object",
            "properties": {
                "node_editor_zoom": {"type": "number"},
                "node_editor_pan": {"type": "object"},
                "selected_node_ids": {"type": "array", "items": {"type": "string"}}
            }
        },
        "deliver_page_state": {
            "type": "object",
            "properties": {
                "selected_preset_id": {"type": ["string", "null"]},
                "render_progress": {"type": "number"}
            }
        },
        "window_state": {
            "type": "object",
            "properties": {
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "maximized": {"type": "boolean"}
            }
        }
    }
}


# ================================================================
# cache_manifest.json Schema
# ================================================================

CACHE_MANIFEST_SCHEMA_V1: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos_cache_manifest_v1",
    "title": "Cache Manifest v1",
    "description": "资产缓存清单",
    "type": "object",
    "properties": {
        "baked_files": {
            "type": "object",
            "description": "烘焙文件清单 {filename: {size: bytes}}",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "size": {"type": "integer"},
                    "asset_id": {"type": "string"},
                    "created_at": {"type": "number"}
                }
            }
        },
        "ai_result_files": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "size": {"type": "integer"},
                    "plugin_instance_id": {"type": "string"},
                    "created_at": {"type": "number"}
                }
            }
        },
        "thumbnail_files": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "size": {"type": "integer"},
                    "asset_id": {"type": "string"}
                }
            }
        },
        "proxy_files": {
            "type": "object",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "size": {"type": "integer"},
                    "asset_id": {"type": "string"},
                    "proxy_quality": {"type": "string"}
                }
            }
        },
        "total_size_bytes": {"type": "integer"},
        "created_at": {"type": "number"}
    }
}


# ================================================================
# Schema Registry
# ================================================================

SCHEMA_REGISTRY_V1: dict[str, dict[str, Any]] = {
    "manifest": MANIFEST_SCHEMA_V1,
    "project": PROJECT_SCHEMA_V1,
    "edit/media_pool": MEDIA_POOL_SCHEMA_V1,
    "edit/bins": BINS_SCHEMA_V1,
    "edit/timelines": TIMELINES_SCHEMA_V1,
    "edit/timeline_sequences": TIMELINE_SEQUENCES_SCHEMA_V1,
    "color/color_stacks": COLOR_STACKS_SCHEMA_V1,
    "composite/graphs": COMPOSITE_GRAPH_SCHEMA_V1,
    "composite/viewer_state": COMPOSITE_VIEWER_STATE_SCHEMA_V1,
    "deliver/presets": DELIVER_PRESETS_SCHEMA_V1,
    "ui/workspace_state": WORKSPACE_STATE_SCHEMA_V1,
    "cache_manifest": CACHE_MANIFEST_SCHEMA_V1,
}


def get_schema(name: str) -> dict[str, Any] | None:
    """
    获取指定的 schema 定义。

    Args:
        name: schema 名称（如 "manifest", "project", "edit/media_pool" 等）

    Returns:
        schema 定义字典，或 None 如果不存在
    """
    return SCHEMA_REGISTRY_V1.get(name)


def validate_json(data: dict[str, Any], schema_name: str) -> bool:
    """
    验证 JSON 数据是否符合指定 schema。

    Args:
        data: JSON 数据字典
        schema_name: schema 名称

    Returns:
        True 如果验证成功，False 如果数据不符合 schema

    Raises:
        RuntimeError: jsonschema 库不可用（AUD-37001）。验证器缺失时
            必须显式失败——静默放行（fail-open）会让外部数据绕过
            schema 校验且无人察觉。
    """
    schema = get_schema(schema_name)
    if schema is None:
        return False

    try:
        import jsonschema
    except ImportError as exc:
        raise RuntimeError(
            "jsonschema is not installed; schema validation cannot be "
            "performed for external data (AUD-37001 fail-closed). "
            "Install jsonschema or avoid calling validate_json."
        ) from exc

    try:
        jsonschema.validate(data, schema)
        return True
    except jsonschema.ValidationError:
        return False
