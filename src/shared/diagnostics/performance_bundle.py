"""
Performance Bundle (P0-10)
一键导出脱敏性能诊断报告（性能总计划 §6 P0-10）。

## 职责

- collect_performance_bundle(): 汇总帧 trace / 引擎遥测 / 像素拷贝计数 /
  资源快照 / GPU pass 时间为一份 JSON 文档。
- export_performance_bundle(): 落盘（默认写到用户选择的路径）。
- 脱敏：移除/遮蔽素材路径、工程路径、用户目录；只保留诊断必需字段
  （计划 §16.3：不上传用户路径、素材内容、项目名称）。

## 用法

```python
from src.shared.diagnostics.performance_bundle import export_performance_bundle

path = export_performance_bundle(output_path=str(dialog.selectedFiles()[0]))
```

Deliver/Quick Export 页可复用同一入口（extra_sections 传入各自 stats）。
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

BUNDLE_SCHEMA_VERSION = 1

#: 必须整体移除的键（值含用户路径或素材身份）。
_REDACT_KEYS = {
    "asset_path", "source_path", "path", "file", "file_path", "output_path",
    "project_path", "project_name", "timeline_name", "sequence_name",
    "name", "user", "home",
}

#: 绝对路径样式（macOS/Windows/POSIX）——命中即遮蔽。
#: 注意 raw string：`\\` 在正则里匹配一个字面反斜杠（C:\Users\...）。
#: AUD-33010: 补齐 macOS 外置卷（/Volumes）、Linux root 用户（/root）
#: 与 Windows UNC 网络盘（\\server\share\...）——这些素材路径样式此前
#: 原样进入"已脱敏"报告外发。
_ABS_PATH_RE = re.compile(
    r"(?:/Users/[^\s'\"]+"
    r"|/Volumes/[^\s'\"]+"
    r"|/home/[^\s'\"]+"
    r"|/root/[^\s'\"]+"
    r"|[A-Za-z]:\\[^\s'\"]+"
    r"|\\\\[^\s'\"]+)"
)


def sanitize_value(value: Any, _depth: int = 0) -> Any:
    """递归脱敏：移除敏感键，遮蔽绝对路径字符串。"""
    if _depth > 12:
        return "<deep>"
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if key.lower() in _REDACT_KEYS:
                out[key] = "<redacted>"
                continue
            out[key] = sanitize_value(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_value(v, _depth + 1) for v in value]
    if isinstance(value, str) and _ABS_PATH_RE.search(value):
        return _ABS_PATH_RE.sub("<path>", value)
    return value


def collect_performance_bundle(
    *,
    trace_collector: Optional[Any] = None,
    engine: Optional[Any] = None,
    gpu_renderer: Optional[Any] = None,
    resource_sampler: Optional[Any] = None,
    extra_sections: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """汇总当前会话的全部性能数据（未脱敏数据不出本函数）。

    Args:
        trace_collector: FrameTraceCollector（帧级 trace + summary）。
        engine: RealtimePlaybackEngine（telemetry.summary() + stats()）。
        gpu_renderer: EditGpuPreviewRenderer（gpu_pass_timer_stats 等）。
        resource_sampler: ResourceSnapshotSampler（资源快照摘要）。
        extra_sections: 其他页面/模块附加段（Deliver 编码吞吐等）。
    """
    from src.shared.video.pixel_copy_counters import snapshot_pixel_copies

    sections: Dict[str, Any] = {}

    if trace_collector is not None:
        try:
            sections["frame_trace_summary"] = trace_collector.get_summary()
            sections["frame_traces"] = [
                t.to_compact_dict() for t in trace_collector.get_traces()
            ]
        except Exception as e:  # noqa: BLE001 诊断不伤业务
            sections["frame_trace_summary"] = {"error": str(e)}

    if engine is not None:
        try:
            telemetry = getattr(engine, "telemetry", None)
            if telemetry is not None and hasattr(telemetry, "summary"):
                sections["engine_telemetry"] = telemetry.summary()
        except Exception as e:  # noqa: BLE001
            sections["engine_telemetry"] = {"error": str(e)}
        try:
            if hasattr(engine, "stats"):
                sections["engine_stats"] = engine.stats()
        except Exception as e:  # noqa: BLE001
            sections["engine_stats"] = {"error": str(e)}

    if gpu_renderer is not None and hasattr(gpu_renderer, "gpu_pass_timer_stats"):
        try:
            sections["gpu_pass_timer"] = gpu_renderer.gpu_pass_timer_stats()
        except Exception as e:  # noqa: BLE001
            sections["gpu_pass_timer"] = {"error": str(e)}

    try:
        sections["pixel_copy_bytes"] = snapshot_pixel_copies()
    except Exception as e:  # noqa: BLE001
        sections["pixel_copy_bytes"] = {"error": str(e)}

    if resource_sampler is not None:
        try:
            sections["resource_snapshot"] = resource_sampler.summary()
        except Exception as e:  # noqa: BLE001
            sections["resource_snapshot"] = {"error": str(e)}

    if extra_sections:
        sections["extra"] = sanitize_value(extra_sections)

    # machine_info 与基准工具共用；tools 目录在打包/非仓库根运行环境下
    # 可能不可导入，降级为最小本机信息（诊断导出绝不因此失败）。
    try:
        from tools.freeze_benchmark_media_manifest import machine_info

        machine = machine_info()
    except Exception:
        machine = {"platform": sys.platform, "python": sys.version.split()[0]}

    return {
        "$schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle": "topos_performance",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": machine,
        "note": (
            "已脱敏：素材/工程路径被遮蔽。本文件用于性能诊断，"
            "不含素材内容与用户身份。"
        ),
        "sections": sanitize_value(sections),
    }


def export_performance_bundle(
    output_path: str,
    **kwargs: Any,
) -> str:
    """收集并落盘脱敏性能报告，返回实际写入路径。"""
    bundle = collect_performance_bundle(**kwargs)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return str(path)


def default_bundle_filename() -> str:
    """默认导出文件名（含时间戳，不含用户信息）。"""
    return f"topos_performance_{time.strftime('%Y%m%d_%H%M%S')}.json"
