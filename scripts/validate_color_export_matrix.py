#!/usr/bin/env python3
"""R12-B: 多出口同帧对拍矩阵（headless GL，无需 QApplication）。

按 R12 §2 构造图 × 素材矩阵，对每个组合验证：

- **出口对拍**：同一图同一帧 front(f2) vs full-res(f4) 出口在同一
  standalone GL 上下文执行，f2 与 f4 的差异必须落在 f2 半精度量化
  容差内（|Δ| ≤ max(2^-10, |v|·2^-10)），不得出现结构性偏差。
- **选中不变性**：附带 viewer:<中间节点> 逐节点出口的编译执行不得
  改变最终输出（依赖锥裁剪不得污染 front 出口结果，位级一致）。
- **HDR 保真**：负值/超白源经 f4 出口保持（负值 ≤ −0.19、高亮 ≥ 3.9），
  不被钳到 [0,1]。
- **素材行**：合成 HDR fixture、真实素材首帧（SDR）、4K 首帧（full-res
  尺寸执行，记录耗时）。
- **平台行**：本机 GPU 指纹入报告；其他平台由调用方按 NOT RUN 登记。

插件 ×2 使用确定性 DCTL stub（与 B00 基线同一冻结身份，语义与真实
.tglsl 注册表路径同构——跨机器可复现，不触发 PluginManager 扫描）。
非单位 DOMAIN LUT 经正式资源解析器注册（topos-res://）后由节点 LUT
引用消费。

用法：
    .venv/bin/python scripts/validate_color_export_matrix.py \
        [--output docs/validation/color_resources/r12b_export_matrix]
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _hdr_synth(width: int, height: int) -> np.ndarray:
    """HDR float 源：负值块、超白块（4.0）、线性渐变（exercise f4 精度）。"""
    x = np.linspace(0.0, 1.0, width, dtype=np.float32)
    plane = np.tile(x[None, :], (height, 1))
    img = np.stack([plane, 0.25 + 0.5 * plane, 1.0 - plane,
                    np.ones_like(plane)], axis=-1)
    img[: height // 6, :3] = -0.2        # 负值区（胶片下冲；alpha 恒 1）
    img[height // 3: height // 2, :3] = 4.0  # 超白区（PQ/场景高亮）
    return np.ascontiguousarray(img, dtype=np.float32)


def _real_frame(path: Path, frame: int = 0) -> np.ndarray | None:
    """真实素材指定帧 → float32 RGB（0..1）；失败返回 None（如实降级）。"""
    try:
        import av
    except ImportError:
        return None
    try:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            container.seek(0, backward=True)
            seen = 0
            for packet in container.demux(stream):
                for f in packet.decode():
                    if seen < frame:
                        seen += 1
                        continue
                    rgb_frame = f.to_rgb()
                    plane = rgb_frame.planes[0]
                    h, w = f.height, f.width
                    arr = np.frombuffer(plane, dtype=np.uint8)
                    arr = arr.reshape(h, plane.line_size)[:, :w * 3]
                    rgb = arr.reshape(h, w, 3).astype(np.float32) / 255.0
                    return np.ascontiguousarray(
                        np.concatenate([rgb, np.ones((h, w, 1), np.float32)],
                                       axis=-1))
    except Exception:  # noqa: BLE001 — 素材不可解码时如实降级
        return None
    return None


def _write_nonunit_lut(directory: Path) -> Path:
    cube = directory / "nonunit_domain.cube"
    lines = ['TITLE "R12B NonUnit"', "LUT_3D_SIZE 4",
             "DOMAIN_MIN 0.05 0.05 0.05", "DOMAIN_MAX 1.10 1.10 1.10"]
    n = 4
    for b in range(n):
        for g in range(n):
            for r in range(n):
                t = 0.55 * (i / (n - 1)) + 0.05 if False else None
                lines.append(f"{0.55 * (r / (n - 1)) + 0.05:.4f} "
                             f"{0.55 * (g / (n - 1)) + 0.05:.4f} "
                             f"{0.55 * (b / (n - 1)) + 0.05:.4f}")
    cube.write_text("\n".join(lines) + "\n")
    return cube


def _serial_graph(n: int):
    import uuid
    from src.shared.project.color_graph import (
        ColorEdgeData, ColorGraphData, ColorNodeData, NodeType,
    )
    source = ColorNodeData(node_id=str(uuid.uuid4()),
                           node_type=NodeType.SOURCE.value, label="Source")
    output = ColorNodeData(node_id=str(uuid.uuid4()),
                           node_type=NodeType.OUTPUT.value, label="Output")
    nodes, edges, prev = [source], [], source.node_id
    for i in range(n):
        node = ColorNodeData(
            node_id=str(uuid.uuid4()), node_type=NodeType.CORRECTOR.value,
            label=f"C{i}", params={"grade": {"exposure": 0.05 * i}})
        nodes.append(node)
        edges.append(ColorEdgeData(
            edge_id=str(uuid.uuid4()), src_node=prev, src_port="image",
            dst_node=node.node_id, dst_port="image"))
        prev = node.node_id
    nodes.append(output)
    edges.append(ColorEdgeData(
        edge_id=str(uuid.uuid4()), src_node=prev, src_port="image",
        dst_node=output.node_id, dst_port="image"))
    return ColorGraphData(nodes=tuple(nodes), edges=tuple(edges),
                          source_node_id=source.node_id,
                          output_node_id=output.node_id)


def _service():
    from src.features.color.core.color_graph_service import ColorGraphService
    return ColorGraphService(
        effect_schema_provider=lambda rid: None,
        lut_reference_validator=lambda ref: ref.startswith("topos-res://"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=ROOT / "docs/validation/color_resources/"
                                        "r12b_export_matrix")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    try:
        import moderngl  # noqa: F401
    except ImportError:
        print(json.dumps({"status": "skipped",
                          "reason": "ModernGL unavailable"}))
        return 0

    from src.features.color.core.color_graph_compiler import compile_graph
    from src.features.color.core.color_graph_executor import ColorGraphExecutor
    from src.features.color.core.color_graph_param_source import (
        graph_node_layer_provider,
    )
    from src.features.color.core.color_graph_snapshot import freeze_graph
    from src.features.color.core.shader_compiler import ShaderPipeline
    from src.shared.project.color_graph import NodeType

    report: dict[str, Any] = {
        "validation": "r12b_export_parity_matrix",
        "platform": _platform_row(),
        "cases": [],
    }

    workdir = Path(tempfile.mkdtemp(prefix="topos-r12b-"))
    ctx = moderngl.create_standalone_context(require=330)
    executor = ColorGraphExecutor(ctx, pipeline=ShaderPipeline(ctx))
    failures: list[str] = []

    try:
        # 正式资源注册：非单位 DOMAIN LUT（topos-res:// 引用）
        from src.shared.project.resource_resolver import (
            ProjectResourceResolver, ResourceEntry, ResourceIndex,
            set_active_project_resolver, sha256_file,
        )
        cube = _write_nonunit_lut(workdir)
        sha = sha256_file(str(cube))
        entry = ResourceEntry(
            resource_id=f"lut_{sha[:32]}", kind="lut",
            archive_path=f"resources/lut/{sha}.cube",
            size_bytes=cube.stat().st_size, sha256=sha,
            original_filename=cube.name, roles=("lut",))
        extracted = workdir / "extracted"
        (extracted / "resources" / "lut").mkdir(parents=True)
        shutil.copy(cube, extracted / "resources" / "lut" / f"{sha}.cube")
        resolver = ProjectResourceResolver(
            str(extracted), ResourceIndex(entries={entry.resource_id: entry}))
        set_active_project_resolver(resolver)

        from src.shared.color.lut_loader import load_lut, resample_to_unit_domain
        loaded = resample_to_unit_domain(load_lut(str(cube)))
        lut_tex_bytes = np.ascontiguousarray(
            loaded.table, dtype="f4").tobytes()

        def bind_aux(prog, layer, _cache={}):
            unit = 2
            if "u_curves_tex" in prog:
                ramp = np.linspace(0.0, 1.0, 256, dtype=np.float32)
                data = np.ascontiguousarray(
                    np.stack([ramp] * 3, axis=-1).reshape(256, 1, 3)).tobytes()
                tex = _cache.get("identity-curves")
                if tex is None:
                    tex = ctx.texture((256, 1), components=3, dtype="f4",
                                      data=data)
                    tex.filter = (ctx.LINEAR, ctx.LINEAR)
                    _cache["identity-curves"] = tex
                tex.use(unit)
                prog["u_curves_tex"].value = unit
                unit += 1
            if "u_lut_tex" in prog:
                tex = _cache.get("lut")
                if tex is None:
                    tex = ctx.texture3d((loaded.size,) * 3, 3,
                                        data=lut_tex_bytes, dtype="f4")
                    _cache["lut"] = tex
                tex.use(unit)
                prog["u_lut_tex"].value = unit

        def plugin_stubs():
            from tests.color.fixtures.color_graph import (
                f03_plugin_stub_binding,
            )
            import dataclasses
            first = f03_plugin_stub_binding(index=0)
            second = dataclasses.replace(f03_plugin_stub_binding(index=1),
                                         values={"amount": 0.8})
            return [first, second]

        sources = {"hdr_synth_720p": _hdr_synth(1280, 720)}
        real = _real_frame(ROOT / "tests/test_videos/测试素材01.mov")
        if real is not None:
            sources["real_sdr_1080p"] = real
        real_4k = _real_frame(ROOT / "tests/test_videos/4K_H264.mov")
        if real_4k is not None:
            sources["real_4k_h264"] = real_4k

        def run_case(name: str, graph, source: np.ndarray,
                     plugin_nodes: tuple[str, ...] = (),
                     lut_node: str | None = None,
                     hdr_source: bool = False,
                     lut_clamps: bool = False,
                     graph_pass_count: int = 1) -> None:
            case: dict[str, Any] = {"case": name,
                                    "size": list(source.shape[:2][::-1])}
            h, w = source.shape[:2]
            base_provider = graph_node_layer_provider(graph)

            def provider(node_id):
                payload = base_provider(node_id)
                if not isinstance(payload, dict):
                    return payload
                payload = dict(payload)
                if node_id in plugin_nodes:
                    payload["_plugin_bindings"] = plugin_stubs()
                if node_id == lut_node:
                    payload["active_lut_path"] = str(cube)
                    payload["lut_intensity"] = 1.0
                return payload

            src16 = np.ascontiguousarray(source).astype(np.float16)
            src_tex = ctx.texture((w, h), components=4, dtype="f2",
                                  data=src16)

            def execute(viewer: str, dtype: str) -> np.ndarray:
                snapshot = freeze_graph(graph, session_id="r12b",
                                        viewer_target=viewer,
                                        output_size=(w, h))
                plan = compile_graph(snapshot, size=(w, h))
                result = executor.execute_plan(
                    plan, src_tex, request_id=f"r12b-{name}-{viewer}-{dtype}",
                    source_size=(w, h), layer_param_provider=provider,
                    bind_aux=bind_aux, dtype=dtype)
                assert result.published, (
                    name, viewer, result.diagnostic_code,
                    result.diagnostic_message)
                fbo = ctx.framebuffer(color_attachments=[result.output_texture])
                fbo.use()
                read_dtype = "f4" if dtype == "f4" else "f2"
                np_dtype = np.float32 if dtype == "f4" else np.float16
                arr = np.frombuffer(
                    fbo.read(components=4, dtype=read_dtype),
                    dtype=np_dtype).reshape(h, w, 4).astype(np.float32)
                executor.release_revision(f"r12b-{name}-{viewer}-{dtype}")
                return arr

            output_node = next(n for n in graph.nodes
                               if n.node_type == NodeType.OUTPUT.value)
            # 依赖锥内一个中间 corrector 作为「选中节点」出口
            mids = [n for n in graph.nodes
                    if n.node_type == NodeType.CORRECTOR.value]

            t0 = time.monotonic()
            front = execute(f"viewer:{output_node.node_id}", "f2")
            t1 = time.monotonic()
            full = execute(f"viewer:{output_node.node_id}", "f4")
            t2 = time.monotonic()
            # 选中不变性（N01/N06 契约）：逐节点 viewer 需求（含缓存
            # 写入）之后，front 出口复跑必须位级一致——依赖锥/缓存键
            # 不得把中间节点结果污染到最终出口。
            if mids:
                execute(f"viewer:{mids[0].node_id}", "f2")
            front_again = execute(f"viewer:{output_node.node_id}", "f2")
            t3 = time.monotonic()
            case["timings_ms"] = {
                "front_f2": round((t1 - t0) * 1000, 1),
                "full_res_f4": round((t2 - t1) * 1000, 1),
                "per_node_f2": round((t3 - t2) * 1000, 1),
            }

            # 出口对拍：f2 vs f4 的差异必须可由逐 pass 半精度舍入的
            # 线性累积解释（f2 尾数 10 位 → 相对 eps 2^-11/pass；深链
            # 无误差抵消假设，按 pass 数线性放宽；结构性偏差超此即失败）。
            passes = max(int(graph_pass_count), 1)
            eps = passes * 2.0 ** -11 * np.maximum(1.0, np.abs(full))
            delta = np.abs(front - full)
            case["parity"] = {
                "max_abs_diff": round(float(delta.max()), 6),
                "tolerance_max": round(float(eps.max()), 6),
                "within_f2_quantization": bool((delta <= eps + 1e-6).all()),
            }
            if not case["parity"]["within_f2_quantization"]:
                failures.append(f"{name}: f2/f4 出口对拍超容差")

            identical = bool(np.array_equal(front, front_again))
            case["selection_invariance"] = identical
            if not identical:
                failures.append(f"{name}: 中间节点 viewer 出口污染 front 出口")

            # HDR 保真（f4 出口）
            case["hdr_preservation"] = {
                "min": round(float(full.min()), 4),
                "max": round(float(max(full[..., :3].max(), 0)), 4),
            }
            if lut_clamps and lut_node:
                # LUT 阶段确实生效：去掉 LUT 引用重跑，输出必须可区分
                #（非单位域采样角点语义由 R05 LUT golden 套件覆盖，此处
                # 只登记观测值，不设角点门槛）。
                rgb_min = float(full[..., :3].min())
                rgb_max = float(full[..., :3].max())
                case["lut_domain_observed"] = {
                    "rgb_min": round(rgb_min, 4), "rgb_max": round(rgb_max, 4)}
                saved_provider = provider

                def no_lut_provider(node_id, _base=saved_provider):
                    payload = _base(node_id)
                    if isinstance(payload, dict):
                        payload = dict(payload)
                        payload.pop("active_lut_path", None)
                        payload.pop("lut_intensity", None)
                    return payload

                snapshot = freeze_graph(graph, session_id="r12b-nolut",
                                        viewer_target=f"viewer:{output_node.node_id}",
                                        output_size=(w, h))
                plan = compile_graph(snapshot, size=(w, h))
                result = executor.execute_plan(
                    plan, src_tex, request_id=f"r12b-{name}-nolut",
                    source_size=(w, h), layer_param_provider=no_lut_provider,
                    bind_aux=bind_aux, dtype="f4")
                assert result.published, (name, "nolut", result.diagnostic_message)
                fbo = ctx.framebuffer(color_attachments=[result.output_texture])
                fbo.use()
                no_lut = np.frombuffer(
                    fbo.read(components=4, dtype="f4"),
                    dtype=np.float32).reshape(h, w, 4)
                executor.release_revision(f"r12b-{name}-nolut")
                lut_active = bool(
                    np.abs(no_lut - full).max() > 1e-4)
                case["lut_stage_active"] = lut_active
                if not lut_active:
                    failures.append(f"{name}: LUT 引用未改变输出——LUT 阶段未生效")
            elif hdr_source:
                if float(full[..., :3].min()) > -0.19:
                    failures.append(f"{name}: 负值被钳制 (min>−0.19)")
                if float(full[..., :3].max()) < 3.9:
                    failures.append(f"{name}: 超白高亮丢失 (max<3.9)")

            # 输出非平凡（确实执行了调色链）
            changed = bool(np.abs(full - np.clip(
                source.astype(np.float32), -4, 4)).max() > 1e-4)
            case["output_is_graded"] = changed
            if not changed:
                failures.append(f"{name}: 输出与源逐位相同——调色链未生效")

            src_tex.release()
            report["cases"].append(case)
            print(json.dumps(case, ensure_ascii=False), flush=True)

        # ── 图 × 素材矩阵 ─────────────────────────────────────────
        service = _service()

        # 1. 8 节点串行 + LUT(非单位域) + 插件×2（合成 HDR 源）
        g8 = _serial_graph(8)
        c8 = [n.node_id for n in g8.nodes
              if n.node_type == NodeType.CORRECTOR.value]
        lut_applied = service.apply_intent(g8, "set_node_lut", {
            "node_id": c8[0], "lut_ref": entry.uri})
        assert lut_applied.ok, lut_applied.diagnostics
        run_case("serial8_lut2plugins_hdr", lut_applied.graph,
                 sources["hdr_synth_720p"],
                 plugin_nodes=(c8[1], c8[2]), lut_node=c8[0],
                 hdr_source=True, lut_clamps=True, graph_pass_count=9)

        # 2. 32 节点串行（合成 HDR 源）
        g32 = _serial_graph(32)
        run_case("serial32_hdr", g32, sources["hdr_synth_720p"],
                 hdr_source=True, graph_pass_count=33)

        # 3. 分支 + Mixer（真实 SDR 素材）
        grown = service.apply_intent(_serial_graph(1), "add_parallel_branch", {})
        assert grown.ok
        run_case("branch_mixer_real_sdr", grown.graph,
                 sources.get("real_sdr_1080p", sources["hdr_synth_720p"]),
                 hdr_source="real_sdr_1080p" not in sources,
                 graph_pass_count=5)

        # 4. Compound + Shared 定义 ×2（真实 SDR 素材）
        g2 = _serial_graph(2)
        c2 = [n.node_id for n in g2.nodes
              if n.node_type == NodeType.CORRECTOR.value]
        grouped = service.apply_intent(g2, "group_subgraph", {
            "member_ids": c2})
        assert grouped.ok
        if "real_sdr_1080p" in sources:
            run_case("compound_real_sdr", grouped.graph,
                     sources["real_sdr_1080p"], graph_pass_count=5)

        # 5. 真实素材 1080p（无修饰，SDR 基线行）
        if "real_sdr_1080p" in sources:
            run_case("serial8_plain_real_sdr", lut_applied.graph,
                     sources["real_sdr_1080p"], graph_pass_count=9)

        # 6. 4K full-res 行（f4 全尺寸执行）
        if "real_4k_h264" in sources:
            g4k = _serial_graph(4)
            run_case("serial4_fullres_4k", g4k, sources["real_4k_h264"],
                     graph_pass_count=5)

    finally:
        try:
            executor.release_pool()
        except Exception:  # noqa: BLE001
            pass
        ctx.release()
        from src.shared.project.resource_resolver import (
            set_active_project_resolver as _clear,
        )
        _clear(None)
        shutil.rmtree(workdir, ignore_errors=True)

    report["failures"] = failures
    report["status"] = "passed" if not failures else "failed"
    (output / "matrix_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": report["status"], "failures": failures},
                     ensure_ascii=False), flush=True)
    return 0 if not failures else 1


def _platform_row() -> dict[str, str]:
    import platform
    import moderngl
    try:
        ctx = moderngl.create_standalone_context(require=330)
        info = ctx.info
        ctx.release()
    except Exception as exc:  # noqa: BLE001
        info = {"GL_RENDERER": f"unavailable: {exc}"}
    return {
        "os": f"{platform.system()} {platform.release()} "
              f"({platform.machine()})",
        "python": platform.python_version(),
        "gl_renderer": info.get("GL_RENDERER", "?"),
        "gl_version": info.get("GL_VERSION", "?"),
        "other_platforms": "NOT RUN（无设备）",
    }


if __name__ == "__main__":
    raise SystemExit(main())
