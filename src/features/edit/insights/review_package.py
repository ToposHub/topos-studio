"""T14 · 本地只读审片资料包（同次捕获的全部资料 + manifest + 可选 ZIP）。

纪律（计划 T14）：

- 包内所有资料来自**同一次 InsightSnapshot**（生成期间继续编辑工程不影
  响包内容）；snapshot.json 用 T01 唯一 codec 完整保存（schema_version、
  工程/序列 revision、payload/content hash、加载范围齐全）——不是只存
  行表或 hash；
- 报告声明 ``report_schema_version``（``report.REPORT_SCHEMA``）；
- 临时目录写全 → 校验 → 原子发布；失败/取消不覆盖旧包；清理只触本任务
  临时文件；
- 缺图可选降级（联系表占位）但 manifest 写明完整性，不静默省略；
- 静态 HTML（转义、本地相对资源、无脚本/无 HTTP 服务/无云）；只导出
  资料不复制原片、不调用 Deliver 编码；无评论写回/上传/工程恢复。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from src.features.edit.insights.codec import serialize_snapshot
from src.features.edit.insights.contact_sheet import (
    build_contact_cells,
    render_contact_sheet_png,
    write_contact_sheet_html,
)
from src.features.edit.insights.dto import InsightSnapshot
from src.features.edit.insights.report import (
    REPORT_SCHEMA,
    build_report,
    write_csv,
    write_html,
    write_json,
)
from src.features.edit.insights.stats import compute_usage_rhythm

PACKAGE_SCHEMA = "insights-review-package/1"


@dataclass(frozen=True)
class PackageResult:
    ok: bool
    directory: str | None
    zip_path: str | None
    manifest: dict | None
    error: str | None = None
    degraded: bool = False  # 缺图降级（占位联系表）但已发布


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rhythm_dict(report) -> dict:
    return {
        s.sequence_id: {
            "name": s.name,
            "load_state": s.load_state,
            "duration_frames": s.duration_frames,
            "clip_count": s.clip_count,
            "video_shots": s.video_shot_rhythm.shot_count if s.video_shot_rhythm else None,
            "mean_shot": s.video_shot_rhythm.mean_length if s.video_shot_rhythm else None,
            "median_shot": s.video_shot_rhythm.median_length if s.video_shot_rhythm else None,
            "cuts_per_minute": s.video_shot_rhythm.cuts_per_minute if s.video_shot_rhythm else None,
            "coverage_union": s.all_tracks_union.union_frames if s.all_tracks_union else None,
            "scope_notes": list(report.scope_notes),
        }
        for s in report.sequences
    }


def _write_index_html(staging: Path, snapshot: InsightSnapshot, degraded: bool) -> str:
    import html as html_module

    def esc(v) -> str:
        return html_module.escape(str(v), quote=True)

    seq_rows = "".join(
        f"<li>{esc(s.name)} · {esc(s.fps.num)}/{esc(s.fps.den)}"
        f"{' DF' if s.fps.drop_frame else ''} · {esc(s.load_state)}"
        f" · dur={esc(s.duration_frames if s.duration_frames is not None else '未收集')}</li>"
        for s in snapshot.sequences
    )
    degrade_note = (
        '<p class="note">联系表含占位格（缩略图不可用），原因见各格。</p>'
        if degraded else ""
    )
    html_text = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<title>Review Package</title><style>"
        "body{font-family:sans-serif;margin:24px;color:#222;line-height:1.5}"
        ".note{background:#fff3cd;padding:8px;border:1px solid #e0c56e}"
        "ul{max-width:720px}a{color:#0366d6}"
        "</style></head><body>"
        f"<h1>{esc(snapshot.project.name)} · 审片资料包</h1>"
        f"<p>capture <code>{esc(snapshot.capture_id)}</code> · "
        f"payload <code>{esc(snapshot.payload_hash or '')[:12]}</code> · "
        f"content <code>{esc(snapshot.content_hash or '')[:12]}</code></p>"
        f"<ul>{seq_rows}</ul>"
        "<h2>内容</h2><ul>"
        "<li><a href=\"report.html\">总表（HTML）</a> · report.csv · report.json</li>"
        "<li><a href=\"stats.html\">统计摘要</a> · stats.json</li>"
        "<li><a href=\"contact_sheet/index.html\">联系表</a>（源缩略图，非最终画面）</li>"
        "<li>snapshot.json（完整只读快照，T01 codec） · manifest.json</li>"
        "</ul>"
        f"{degrade_note}"
        "<p class=\"note\">本包为只读审片资料：无评论写回、不上传、不能恢复工程；"
        "源素材路径不包含在包内。</p>"
        "</body></html>"
    )
    path = staging / "index.html"
    path.write_text(html_text, encoding="utf-8")
    return str(path.name)


def _write_stats_html(staging: Path, rhythm_report) -> str:
    import html as html_module

    def esc(v) -> str:
        return html_module.escape(str(v), quote=True)

    rows = "".join(
        "<tr>" + "".join(
            f"<td>{esc(cell)}</td>" for cell in (
                s.name, s.load_state,
                s.duration_frames if s.duration_frames is not None else "—",
                s.video_shot_rhythm.shot_count if s.video_shot_rhythm else "—",
                f"{s.video_shot_rhythm.mean_length:.1f}" if s.video_shot_rhythm and s.video_shot_rhythm.mean_length else "—",
                f"{s.video_shot_rhythm.cuts_per_minute:.2f}" if s.video_shot_rhythm and s.video_shot_rhythm.cuts_per_minute else "—",
                s.all_tracks_union.union_frames if s.all_tracks_union else "—",
            )
        ) + "</tr>"
        for s in rhythm_report.sequences
    )
    notes = "".join(f"<li>{esc(n)}</li>" for n in rhythm_report.scope_notes)
    html_text = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<title>Stats</title><style>"
        "body{font-family:sans-serif;margin:24px;color:#222}"
        "table{border-collapse:collapse}td,th{border:1px solid #bbb;"
        "padding:4px 10px;font-size:13px}"
        "</style></head><body><h1>统计摘要</h1>"
        "<table><tr><th>序列</th><th>加载</th><th>时长</th><th>镜头</th>"
        "<th>均值</th><th>切点/分</th><th>覆盖并集</th></tr>"
        f"{rows}</table><h3>口径</h3><ul>{notes}</ul></body></html>"
    )
    (staging / "stats.html").write_text(html_text, encoding="utf-8")
    return "stats.html"


def build_review_package(
    snapshot: InsightSnapshot,
    output_dir: str | Path,
    *,
    host: object | None = None,
    include_contact_sheet: bool = True,
    make_zip: bool = False,
    contact_sequence_ids: tuple[str, ...] = (),
    audio_cue_state_fn: object | None = None,
) -> PackageResult:
    """把同一次捕获打成资料目录（可选 ZIP）。

    输出布局：``<output_dir>/<name>-<capture8>/``；staging 目录为
    ``<output_dir>/.staging-<capture8>-<pid>/``，写全→校验→改名发布；
    任何失败清理 staging，旧包不动。

    ``audio_cue_state_fn``：提供时（InjectedHostAdapter.capture_state_fn）
    为每个含音频轨的已加载序列附 ``audio_cues_*.csv``（复用既有
    interchange.CueSheet，见 audio_cues 模块）；缺省跳过，不伪造。
    """
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    final_dir = output / f"insights-pkg-{snapshot.capture_id[:8]}"
    staging = output / f".staging-{snapshot.capture_id[:8]}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        report = build_report(snapshot)
        rhythm = compute_usage_rhythm(snapshot)

        write_csv(report, str(staging / "report.csv"))
        write_json(report, str(staging / "report.json"))
        write_html(report, str(staging / "report.html"))
        (staging / "stats.json").write_text(
            json.dumps(_rhythm_dict(rhythm), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        _write_stats_html(staging, rhythm)
        (staging / "snapshot.json").write_text(
            serialize_snapshot(snapshot), encoding="utf-8"
        )
        if callable(audio_cue_state_fn):
            from src.features.edit.insights.audio_cues import export_audio_cue_csvs

            export_audio_cue_csvs(audio_cue_state_fn, snapshot, staging)

        degraded = False
        if include_contact_sheet:
            sheet_dir = staging / "contact_sheet"
            sheet_dir.mkdir()
            ids = contact_sequence_ids or tuple(
                s.sequence_id for s in snapshot.sequences
                if s.load_state == "loaded" and any(
                    t.track_type == "video" for t in s.tracks
                )
            )
            png_names: list[str] = []
            for seq_id in ids:
                cells = build_contact_cells(snapshot, seq_id, host=host)
                if not cells:
                    continue
                name = f"sheet_{seq_id[:12]}.png"
                render_contact_sheet_png(cells, str(sheet_dir / name), columns=3)
                write_contact_sheet_html(
                    cells, str(sheet_dir), prefix=f"cs_{seq_id[:12]}"
                )
                png_names.append(name)
                if any(c.thumb_quality != "source_thumbnail" for c in cells):
                    degraded = True
            if not png_names:
                degraded = True  # 无任何联系表（如全音频序列）也如实标注
                (sheet_dir / "index.html").write_text(
                    "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
                    "</head><body><p>无视频镜头：联系表未生成。</p></body></html>",
                    encoding="utf-8",
                )

        _write_index_html(staging, snapshot, degraded)

        # manifest：文件清单 + sha256 + 完整性声明
        files: list[dict] = []
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                files.append({
                    "path": path.relative_to(staging).as_posix(),
                    "sha256": _sha256_file(path),
                    "bytes": path.stat().st_size,
                })
        manifest = {
            "schema": PACKAGE_SCHEMA,
            "report_schema_version": REPORT_SCHEMA,
            "snapshot_schema_version": snapshot.schema_version,
            "capture_id": snapshot.capture_id,
            "captured_at": snapshot.captured_at,
            "project": {"id": snapshot.project.project_id, "name": snapshot.project.name,
                        "revision": snapshot.project.revision},
            "sequence_revisions": snapshot.sequence_revision_map,
            "payload_hash": snapshot.payload_hash,
            "content_hash": snapshot.content_hash,
            "load_states": {s.sequence_id: s.load_state for s in snapshot.sequences},
            "contact_sheet": {
                "included": include_contact_sheet,
                "degraded": degraded,
                "note": "source stills, not final color/composite frames",
            },
            "read_only": True,
            "files": files,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )

        # 原子发布：staging 改名为最终目录（同一文件系统 rename）
        if final_dir.exists():
            # 旧包不覆盖：本次发布挂起失败并报告
            return PackageResult(
                ok=False, directory=None, zip_path=None, manifest=None,
                error=f"target package already exists: {final_dir}", degraded=degraded,
            )
        os.rename(staging, final_dir)

        zip_path: str | None = None
        if make_zip:
            zip_target = output / f"{final_dir.name}.zip"
            if zip_target.exists():
                return PackageResult(
                    ok=True, directory=str(final_dir), zip_path=None,
                    manifest=manifest, error=f"zip exists, left directory only: {zip_target}",
                    degraded=degraded,
                )
            with zipfile.ZipFile(zip_target, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(final_dir.rglob("*")):
                    if path.is_file():
                        zf.write(path, path.relative_to(final_dir).as_posix())
            zip_path = str(zip_target)

        return PackageResult(
            ok=True, directory=str(final_dir), zip_path=zip_path,
            manifest=manifest, degraded=degraded,
        )
    except Exception as exc:
        # 失败：清理自己的 staging，不影响旧包/最终目录
        shutil.rmtree(staging, ignore_errors=True)
        return PackageResult(
            ok=False, directory=None, zip_path=None, manifest=None,
            error=str(exc),
        )
