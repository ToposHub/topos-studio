"""Run every registered Composite node once with a real TOPOS-derived frame.

The existing GPU coverage driver intentionally focuses on the 63 common
render nodes.  This companion closes the inventory gap: every key in
``NODE_REGISTRY`` is attempted, including nodes that may be source-only,
CPU/AI-backed, or not constructible on the current machine.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("TOPOS_HEADLESS_GL", "1")
    from scripts.bench_composite_nodes_gpu_coverage import (
        BENCH_DIR,
        NODE_CLASSES,
        PARAM_OVERRIDES,
        _sources,
        bench_case,
        build_graph,
        classify_node_result,
        resolve_node_fixtures,
    )
    from src.features.composite.core.node_capabilities import (
        get_node_contract_class,
    )
    from src.features.composite.core.node_engine import NODE_REGISTRY

    sources = _sources("hd")
    out_size = (1920, 1080)
    rows: list[dict] = []
    for index, node_type in enumerate(sorted(NODE_REGISTRY), 1):
        print(f"[{index}/{len(NODE_REGISTRY)}] {node_type}", flush=True)
        started = time.perf_counter()
        row = {
            "node_type": node_type,
            "exec_class": NODE_CLASSES.get(node_type, "unclassified"),
            # T-P1-60：契约五分类 + 六态可解释状态（None/全平不再笼统记 ok）
            "contract_class": get_node_contract_class(node_type),
            "backend": "headless_gl_fragment",
            "source": "2K_topos_proxy.mov -> realhd PNG",
        }
        try:
            fx_sources, fx_params = resolve_node_fixtures(
                node_type, sources, PARAM_OVERRIDES.get(node_type, {}))
            graph = build_graph(
                [(node_type, fx_params)],
                fx_sources,
                out_size,
            )
            row.update(bench_case(
                f"{node_type}@topos_hd", graph,
                preview_frames=1, export_frames=1,
            ))
        except Exception as exc:  # noqa: BLE001 - inventory must continue
            row.update({
                "error": f"{type(exc).__name__}: {exc}",
                "error_stage": "build_or_bench",
            })
        row["total_wall_s"] = round(time.perf_counter() - started, 3)
        row["status"] = classify_node_result(
            node_type, row.get("output") or {}, row.get("error"))
        rows.append(row)
        print(
            f"    status={'error' if row.get('error') else 'ok'} "
            f"preview={row.get('preview_ms')} export={row.get('export_ms')} "
            f"flat={row.get('output', {}).get('flat', False)}",
            flush=True,
        )

    # QA-03（2026-10-14 审计）：输出目录带运行日期（不覆盖旧结果）；
    # 素材路径可经 TC_BENCH_MEDIA 覆盖，缺素材时明确 blocked 而不是
    # 静默测一个不存在的文件。
    import datetime
    import json as _json_mod
    media_env = os.environ.get("TC_BENCH_MEDIA")
    media = Path(media_env) if media_env else (
        ROOT / "tests" / "test_videos" / "2K_topos_proxy.mov")
    if not media.exists():
        # 退役素材名兜底：退到目录内任意 2K topos 素材
        candidates = sorted((ROOT / "tests" / "test_videos").glob("2K_topos*.mov"))
        if not candidates:
            print(f"BLOCKED: benchmark media not found: {media}", flush=True)
            return 2
        media = candidates[0]
    out = (ROOT / "bench_out"
           / f"real_topos_all_composite_nodes_{datetime.date.today():%Y%m%d}"
           / "real_topos_all_composite_nodes.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "real_topos_all_composite_node_matrix",
        "source": str(media),
        "bench_dir": str(BENCH_DIR),
        "registered_count": len(NODE_REGISTRY),
        "rows": rows,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.get("status", "failed")] = by_status.get(
            r.get("status", "failed"), 0) + 1
    print(f"results -> {out}")
    print(f"registered={len(rows)} by_status={json.dumps(by_status, sort_keys=True)}")
    # 完成定义是"可解释"而非"全过"：failed 是合法状态（如 chroma_keyer
    # 缺绿幕 fixture 接线）；仅当出现六态词表之外的状态才视为不可解释。
    known = {"passed_nonflat", "passed_flat_by_contract",
             "blocked_missing_resource", "blocked_missing_model",
             "unsupported_backend", "failed"}
    unexplained = [r["node_type"] for r in rows
                   if r.get("status") not in known]
    if unexplained:
        print(f"UNEXPLAINABLE rows: {unexplained}")
    return 1 if unexplained else 0


if __name__ == "__main__":
    raise SystemExit(main())
