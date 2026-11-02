#!/usr/bin/env python
"""B00 旧引擎 float reference 生成器（调色节点后端迁移基线）。

用法::

    .venv/bin/python scripts/generate_color_graph_baselines.py \
        --output docs/validation/color_nodes/baselines

产出：
  - ``f01_neutral_<size>.npy`` / ``f03_stack_<variant>.npy``：旧引擎
    （生产 composition shader + RGBA16F ping-pong 精度）的 float32 参考；
  - ``manifest.json``：代码版本（git SHA）、机器/GPU 指纹、颜色上下文、
    尺寸、后端、误差定义、每份数据的 SHA256 与统计（min/max/mean）。

只读生成：不修改工程、不写用户配置；输出目录由参数指定。GL 不可用时
以非零码退出并显式说明（不产生伪 golden）。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.color.fixtures.color_graph import (  # noqa: E402
    ERROR_CONTRACT,
    FIXTURE_SPEC_VERSION,
    f01_hdr_ramp,
    f02_patches,
    f03_layer_stack,
    f03_plugin_stub_binding,
    fixture_manifest,
)

W, H = 64, 64  # 基线统一尺寸（计划 B00：64×64）


def _git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "unknown"


def _worktree_dirty() -> bool:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=30)
        return bool(proc.stdout.strip()) if proc.returncode == 0 else False
    except (OSError, subprocess.TimeoutExpired):
        return False


def _gpu_fingerprint(ctx: Any) -> Dict[str, str]:
    info = ctx.info
    return {
        "renderer": info.get("GL_RENDERER", "unknown"),
        "vendor": info.get("GL_VENDOR", "unknown"),
        "version": info.get("GL_VERSION", "unknown"),
    }


def _quad(ctx: Any, prog: Any) -> Any:
    """全屏 quad（TRIANGLE_STRIP，in_vert + in_texcoord）。"""
    return ctx.vertex_array(
        prog,
        [(ctx.buffer(np.array([
            -1.0, -1.0, 0.0, 0.0,
            1.0, -1.0, 1.0, 0.0,
            -1.0, 1.0, 0.0, 1.0,
            1.0, 1.0, 1.0, 1.0,
        ], dtype="f4").tobytes()), "2f 2f", "in_vert", "in_texcoord")],
    )


def _render_layer_pass(
    ctx: Any,
    pipeline: Any,
    source: np.ndarray,
    layer: Any,
    bindings: Sequence[Any] = (),
    flags: Dict[str, bool] | None = None,
) -> np.ndarray:
    """单层 composition pass：上传源/白 matte，执行，读回 float32。

    与生产 async_renderer ping-pong 同精度（RGBA16F 目标、f2 读回）。
    """
    from src.features.color.core.composition_uniforms import (
        composition_uniform_values,
        upload_composition_uniforms,
    )

    h, w = source.shape[:2]
    flags = flags or {}
    prog = pipeline.build_composition_shader(
        has_curves=flags.get("has_curves", False),
        has_lut3d=flags.get("has_lut3d", False),
        has_soft_clip=flags.get("has_soft_clip", False),
        has_qualifier=False,  # qualifier 在 matte 生成阶段，此处白 matte
        has_mixer=flags.get("has_mixer", False),
        plugins=list(bindings) or None,
    )
    if prog is None:
        raise RuntimeError("build_composition_shader 返回 None（编译失败）")

    src_tex = ctx.texture((w, h), components=4, dtype="f2",
                          data=np.ascontiguousarray(source).astype(np.float16))
    out_tex = ctx.texture((w, h), components=4, dtype="f2")
    matte = np.ones((h, w, 4), dtype=np.float16)
    matte_tex = ctx.texture((w, h), components=4, dtype="f2", data=matte)
    fbo = ctx.framebuffer(color_attachments=[out_tex])

    prog["u_input_tex"].value = 0
    prog["u_matte_tex"].value = 1
    src_tex.use(0)
    matte_tex.use(1)

    # 与生产 _bind_aux_textures_single_layer 一致：u_curves_tex 在 unit 2，
    # 无曲线层绑 identity ramp（避免空采样器产生未定义数据）。
    curves_tex = None
    if "u_curves_tex" in prog:
        lut = getattr(layer, "curves_lut", None)
        if lut is None:
            ramp = np.linspace(0.0, 1.0, 256, dtype=np.float32)
            data = np.stack([ramp] * 3, axis=-1).reshape(256, 1, 3)
        else:
            data = np.asarray(lut, dtype=np.float32).reshape(256, 1, 3)
        curves_tex = ctx.texture((256, 1), components=3, dtype="f4",
                                 data=np.ascontiguousarray(data).tobytes())
        curves_tex.filter = (ctx.LINEAR, ctx.LINEAR)
        curves_tex.use(2)
        prog["u_curves_tex"].value = 2

    values = composition_uniform_values(layer, bindings=tuple(bindings),
                                        width=w, height=h)
    upload_composition_uniforms(prog, values)

    fbo.use()
    _quad(ctx, prog).render(moderngl_triangle_strip(ctx))
    raw = np.frombuffer(fbo.read(components=4, dtype="f2"),
                        dtype=np.float16).reshape(h, w, 4)
    result = raw.astype(np.float32)

    for tex in (src_tex, out_tex, matte_tex, curves_tex):
        if tex is not None:
            tex.release()
    fbo.release()
    return np.ascontiguousarray(result)


def moderngl_triangle_strip(ctx: Any) -> Any:
    import moderngl
    return moderngl.TRIANGLE_STRIP


def _stats(a: np.ndarray) -> Dict[str, Any]:
    rgb = a[..., :3]
    return {
        "sha256": __import__("hashlib").sha256(
            np.ascontiguousarray(a).tobytes()).hexdigest(),
        "min": float(rgb.min()), "max": float(rgb.max()),
        "mean": float(rgb.mean()),
        "alpha_min": float(a[..., 3].min()),
        "alpha_max": float(a[..., 3].max()),
    }


def generate(output_dir: Path) -> Dict[str, Any]:
    import moderngl

    output_dir.mkdir(parents=True, exist_ok=True)
    ctx = moderngl.create_standalone_context(require=330)
    try:
        from src.features.color.core.shader_compiler import ShaderPipeline
        pipeline = ShaderPipeline(ctx)

        from src.features.color.core.layer import Layer
        manifest: Dict[str, Any] = {
            "generated_at_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "git_sha": _git_sha(),
            "worktree_dirty": _worktree_dirty(),
            "platform": {
                "machine": platform.machine(),
                "system": platform.system(),
                "release": platform.release(),
                "python": platform.python_version(),
                "numpy": np.__version__,
            },
            "gpu": _gpu_fingerprint(ctx),
            "backend": {
                "kind": "moderngl standalone context (GL 330 core)",
                "precision": "RGBA16F ping-pong parity with async_renderer",
                "readback": "float16 -> float32 stored",
            },
            "color_context": fixture_manifest()["color_context"],
            "error_contract": ERROR_CONTRACT,
            "fixture_spec_version": FIXTURE_SPEC_VERSION,
            "fixtures": fixture_manifest(),
            "references": {},
        }

        # F01：neutral 恒等（全部默认参数的 Layer）——计划 B00 断言基线之一
        neutral = Layer("neutral")
        for size in (64, 256):
            src = f01_hdr_ramp(size)
            out = _render_layer_pass(ctx, pipeline, src, neutral)
            name = f"f01_neutral_{size}"
            np.save(output_dir / f"{name}.npy", out)
            manifest["references"][name] = _stats(out)

        # B00 实测：neutral 非位级恒等的漂移量化（供 B07 等价阈值冻结）
        src = f01_hdr_ramp(64)
        out = _render_layer_pass(ctx, pipeline, src, neutral)
        diff = np.abs(out[..., :3] - src[..., :3])
        sdr = np.abs(src[..., :3]) <= 1.0
        manifest["neutral_drift"] = {
            "note": "生产 composition shader neutral 参数下的数值噪声（非位级恒等）",
            "sdr_abs_max": float(diff[sdr].max()) if sdr.any() else 0.0,
            "hdr_abs_max": float(diff[~sdr].max()) if (~sdr).any() else 0.0,
            "hdr_rel_max": float(
                (diff[~sdr] / np.maximum(np.abs(src[..., :3][~sdr]), 1e-6)).max()
            ) if (~sdr).any() else 0.0,
        }

        # F01：单层 F03 L0（乘性调色）在 ramp 上
        l0 = f03_layer_stack()[0]
        out = _render_layer_pass(ctx, pipeline, f01_hdr_ramp(64), l0)
        np.save(output_dir / "f01_l0_grading.npy", out)
        manifest["references"]["f01_l0_grading"] = _stats(out)

        # F02：neutral pass（alpha 政策锚点：透明边 + alpha 梯度不被破坏）
        out = _render_layer_pass(ctx, pipeline, f02_patches(64), neutral)
        np.save(output_dir / "f02_neutral.npy", out)
        manifest["references"]["f02_neutral"] = _stats(out)

        # F03：三层串行（生产同序 ping-pong：L0 -> L1 -> L2）
        stack = f03_layer_stack()
        current = f01_hdr_ramp(64)
        for i, layer in enumerate(stack):
            flags = {"has_curves": layer.curves_lut is not None}
            current = _render_layer_pass(
                ctx, pipeline, current, layer,
                bindings=(f03_plugin_stub_binding(),) if i == 2 else (),
                flags=flags)
        np.save(output_dir / "f03_serial.npy", current)
        manifest["references"]["f03_serial"] = _stats(current)

        # F03 变体：L1 旁路（E03 语义：invisible 层整层跳过，非 shader 内
        # 混合——跳层逻辑住在渲染循环；B07.5 修正了初版基线未跳层的缺陷）
        current = f01_hdr_ramp(64)
        for layer in f03_layer_stack():
            if layer.name == "Lift Shadows":
                layer.visible = False
            if not layer.visible:
                continue  # E03 _do_render_impl 同语义
            flags = {"has_curves": layer.curves_lut is not None}
            bindings = (f03_plugin_stub_binding(),) \
                if layer.name == "Tone Curve" else ()
            current = _render_layer_pass(ctx, pipeline, current, layer,
                                         bindings=bindings, flags=flags)
        np.save(output_dir / "f03_l1_bypassed.npy", current)
        manifest["references"]["f03_l1_bypassed"] = _stats(current)

        with open(output_dir / "manifest.json", "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, ensure_ascii=False, sort_keys=True)

        # F08 轻量性能基线（同机对照）：1080p neutral pass 300 次采样。
        # 完整媒体 decode/graph/present 分段 bench 由 B16 scripts/bench_
        # color_graph.py 承担；此处只为"同机旧路径单 pass"锚点。
        try:
            import time as _time
            big = np.random.default_rng(7).random((1080, 1920, 4),
                                                  dtype=np.float32)
            for _ in range(10):  # 预热（shader 编译 + 缓存）
                _render_layer_pass(ctx, pipeline, big, neutral)
            samples = []
            for _ in range(300):
                t0 = _time.perf_counter()
                _render_layer_pass(ctx, pipeline, big, neutral)
                samples.append((_time.perf_counter() - t0) * 1000.0)
            arr = np.asarray(samples)
            manifest["perf_f08_light"] = {
                "workload": "1920x1080 RGBA16F neutral composition pass x300",
                "p50_ms": float(np.percentile(arr, 50)),
                "p95_ms": float(np.percentile(arr, 95)),
                "p99_ms": float(np.percentile(arr, 99)),
                "note": "standalone ctx 合成 pass 锚点，非整管线 FPS；媒体级 bench 见 B16",
            }
            with open(output_dir / "manifest.json", "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, indent=2, ensure_ascii=False, sort_keys=True)
        except Exception as exc:  # noqa: BLE001
            manifest["perf_f08_light"] = {"status": "NOT RUN", "reason": str(exc)}
            with open(output_dir / "manifest.json", "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, indent=2, ensure_ascii=False, sort_keys=True)
        return manifest
    finally:
        ctx.release()
        try:
            from src.features.color.core.shader_compiler import ShaderPipeline  # noqa: F401
        except Exception:  # pragma: no cover
            pass


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=REPO_ROOT / "docs/validation/color_nodes/baselines")
    args = parser.parse_args(argv)

    try:
        manifest = generate(args.output)
    except Exception as exc:  # noqa: BLE001 —— 脚本显式失败，不产生伪基线
        print(f"[baseline] FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"[baseline] wrote {len(manifest['references'])} references "
          f"-> {args.output}")
    for name, stats in manifest["references"].items():
        print(f"  {name}: sha={stats['sha256'][:12]} "
              f"min={stats['min']:.4f} max={stats['max']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
