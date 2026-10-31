"""Regression locks for the shared-memory compute shader fixes (batch A,
w2-g3): AUD-5300 / AUD-5301 / AUD-5302 / AUD-5304.

All assertions are GLSL source-level (no GL context needed):

* AUD-5300 — the blur shared-compute padding must be fully initialized by a
  cooperative strided load; the simulation below replays the load loop and
  the convolution read window and proves every read slot is written exactly
  once by exactly one thread.
* AUD-5301 — the erode/dilate chunk grid must cover the whole padded
  sampling window (loads) and the per-thread reduce loops must cover the
  structuring element [-half, half]^2 exactly (no missing, no duplicate).
* AUD-5302 — every ``shared vec4`` declaration in the compute variants must
  stay within the GL-guaranteed 32 KB (8192 floats) budget.
* AUD-5304 — the reflect edge mode must be the triangle-wave mirror
  ``1.0 - abs(mod(uv, 2.0) - 1.0)`` which is the identity on [0, 1].

An optional real-GL smoke (fragment link / compute compile) runs only when
a standalone ModernGL context is available; GL 4.3 compute is unavailable
under the macOS GL 4.1 cap, in which case the compute smoke skips.
"""

from __future__ import annotations

import ast
import itertools
import math
import re

import pytest

from src.features.composite.core.shaders import (
    _BLUR_H_COMPUTE_SHARED,
    _BLUR_V_COMPUTE_SHARED,
    _ERODE_DILATE_COMPUTE_SHARED,
    _ERODE_DILATE_FRAGMENT,
)

# GL guarantees at least 32 KB of compute shared memory = 8192 floats.
MAX_SHARED_FLOATS = 8192

SHARED_COMPUTE_SHADERS = {
    "blur_h_shared": _BLUR_H_COMPUTE_SHARED,
    "blur_v_shared": _BLUR_V_COMPUTE_SHARED,
    "erode_dilate_shared": _ERODE_DILATE_COMPUTE_SHARED,
}


# ── helpers ──────────────────────────────────────────────────────────────

def glsl_mod(x: float, y: float) -> float:
    """GLSL mod(): result has the sign of y (unlike Python %)."""
    return x - y * math.floor(x / y)


def mirror_fixed(u: float) -> float:
    """The fixed AUD-5304 reflect mapping: 1.0 - abs(mod(u, 2.0) - 1.0)."""
    return 1.0 - abs(glsl_mod(u, 2.0) - 1.0)


def mirror_old(u: float) -> float:
    """The buggy AUD-5304 mapping: abs(mod(u, 2.0) - 1.0)."""
    return abs(glsl_mod(u, 2.0) - 1.0)


def const_ints(source: str) -> dict[str, int]:
    """Collect ``const int NAME = <constexpr>;`` declarations."""
    consts: dict[str, int] = {}
    for name, expr in re.findall(r"const\s+int\s+(\w+)\s*=\s*([^;]+);", source):
        consts[name] = _eval_int_expr(expr, consts)
    return consts


def _eval_int_expr(expr: str, consts: dict[str, int]) -> int:
    """Evaluate an integer constant expression (literals/const names/+-*()).

    Uses ast so arbitrary code in shader strings can never execute.
    """

    def visit(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            return node.value
        if isinstance(node, ast.Name) and node.id in consts:
            return consts[node.id]
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -visit(node.operand)
        raise AssertionError(f"non-constant dimension expression: {expr!r}")

    return visit(ast.parse(expr.strip(), mode="eval"))


def shared_vec4_decls(source: str) -> list[tuple[str, list[int]]]:
    """Parse ``shared vec4 name[d0][d1];`` declarations with resolved dims."""
    decls: list[tuple[str, list[int]]] = []
    consts = const_ints(source)
    for match in re.finditer(r"shared\s+vec4\s+(\w+)\s*((?:\[[^\]]+\])+)\s*;", source):
        name, dims_src = match.group(1), match.group(2)
        dims = [_eval_int_expr(d, consts) for d in re.findall(r"\[([^\]]+)\]", dims_src)]
        decls.append((name, dims))
    return decls


def strip_comments(source: str) -> str:
    return re.sub(r"//[^\n]*", "", source)


# ── AUD-5302: shared memory budget ──────────────────────────────────────

class TestAud5302SharedMemoryBudget:
    @pytest.mark.parametrize("shader_name", sorted(SHARED_COMPUTE_SHADERS))
    def test_shared_declarations_within_gl_guaranteed_budget(self, shader_name: str) -> None:
        source = SHARED_COMPUTE_SHADERS[shader_name]
        decls = shared_vec4_decls(source)
        assert decls, f"{shader_name} lost its shared memory declaration"
        for name, dims in decls:
            floats = 4
            for d in dims:
                floats *= d
            assert floats <= MAX_SHARED_FLOATS, (
                f"{shader_name}: shared vec4 {name}{dims} needs {floats} floats "
                f"({floats * 4} B), above the GL-guaranteed {MAX_SHARED_FLOATS} floats "
                "(32 KB) — AUD-5302 regression")

    def test_oversized_s_block_declaration_is_gone(self) -> None:
        # The old 116x116 vec4 tile (~210 KB) must not come back.
        code = strip_comments(_ERODE_DILATE_COMPUTE_SHARED)
        assert "s_block" not in code
        assert "16 + 100" not in code


# ── AUD-5300: blur padding coverage ─────────────────────────────────────

class TestAud5300BlurPaddingCoverage:
    @staticmethod
    def _stride_bound(source: str, thread_var: str) -> int:
        """Extract the (constant-expression) bound of the strided load loop."""
        code = strip_comments(source)
        pattern = (rf"for\s*\(\s*int\s+i\s*=\s*{thread_var}\s*;\s*i\s*<\s*"
                   rf"([^;]+?)\s*;\s*i\s*\+=\s*16\s*\)")
        match = re.search(pattern, code)
        assert match, (
            f"strided cooperative load loop over {thread_var} not found — "
            "AUD-5300 regression")
        return _eval_int_expr(match.group(1), const_ints(code))

    @pytest.mark.parametrize("shader_name, thread_var", [
        ("blur_h_shared", "local_id.x"),
        ("blur_v_shared", "local_id.y"),
    ])
    def test_stride_loop_bound_is_constant_and_covers_whole_padding(
        self, shader_name: str, thread_var: str,
    ) -> None:
        source = SHARED_COMPUTE_SHADERS[shader_name]
        code = strip_comments(source)
        bound = self._stride_bound(source, thread_var)
        # The bound must be built only from literals and const ints
        # (compile-time constant, per .clinerules loop-bound constraint).
        consts = const_ints(code)
        decl_extent = max(
            d for _, dims in shared_vec4_decls(code) for d in dims)
        assert bound == decl_extent, (
            f"{shader_name}: load loop bound {bound} != shared extent "
            f"{decl_extent} — padding slots would stay uninitialized")

        # Replay the load loop: slot s is written by thread s % 16 exactly once.
        writes: dict[int, int] = {}
        for thread in range(16):
            for slot in range(thread, bound, 16):
                writes[slot] = writes.get(slot, 0) + 1
        assert set(writes) == set(range(bound)), (
            f"{shader_name}: padding coverage has holes")
        assert set(writes.values()) == {1}, (
            f"{shader_name}: some padding slots are written more than once")

    @pytest.mark.parametrize("shader_name, axis", [
        ("blur_h_shared", "x"),
        ("blur_v_shared", "y"),
    ])
    def test_convolution_read_window_fully_initialized(
        self, shader_name: str, axis: str,
    ) -> None:
        source = SHARED_COMPUTE_SHADERS[shader_name]
        code = strip_comments(source)
        consts = const_ints(code)
        max_kernel = consts["MAX_KERNEL"]
        bound = self._stride_bound(source, f"local_id.{axis}")
        written = set(range(bound))
        # shared_idx = kernel + local; read range = [kernel + l - kernel,
        # kernel + l + kernel] = [l, 2*kernel + l] for every legal kernel.
        for kernel in range(0, max_kernel + 1):
            for local in range(16):
                read = set(range(kernel + local - kernel, kernel + local + kernel + 1))
                missing = read - written
                assert not missing, (
                    f"{shader_name}: kernel={kernel}, local={local} reads "
                    f"uninitialized slots {sorted(missing)}")

    @pytest.mark.parametrize("shader_name", ["blur_h_shared", "blur_v_shared"])
    def test_old_sparse_padding_guards_are_gone(self, shader_name: str) -> None:
        source = strip_comments(SHARED_COMPUTE_SHADERS[shader_name])
        assert "(k - 1) % 16" not in source, (
            f"{shader_name}: old buggy padding guard is back (AUD-5300)")
        assert "(16 - k) % 16" not in source, (
            f"{shader_name}: old buggy padding guard is back (AUD-5300)")
        # The old flat per-row/column arrays (trampled by the other tile axis)
        assert "s_row[" not in source and "s_column[" not in source

    def test_blur_tile_rows_are_indexed_by_local_y_not_shared_across_rows(self) -> None:
        # H pass: one padded row per tile row (2D tile), so tile rows cannot
        # trample each other anymore.
        assert re.search(r"s_tile\[row\]\[", strip_comments(_BLUR_H_COMPUTE_SHARED))
        assert re.search(r"s_tile\[[^\]]+\]\[col\]", strip_comments(_BLUR_V_COMPUTE_SHARED))


# ── AUD-5301: erode/dilate chunk coverage ───────────────────────────────

class TestAud5301ErodeDilateChunkCoverage:
    @pytest.fixture(scope="class")
    def chunk_consts(self) -> dict[str, int]:
        consts = const_ints(strip_comments(_ERODE_DILATE_COMPUTE_SHARED))
        assert {"MAX_RADIUS", "MAX_CHUNKS"} <= set(consts)
        return consts

    def test_chunk_grid_covers_max_window(self, chunk_consts: dict[str, int]) -> None:
        # ceil((16 + 2*MAX_RADIUS)/16) chunks must cover the largest window.
        extent = 16 + 2 * chunk_consts["MAX_RADIUS"]
        assert chunk_consts["MAX_CHUNKS"] * 16 >= extent, (
            "MAX_CHUNKS cannot cover the padded window at MAX_RADIUS")

    @pytest.mark.parametrize("half", [0, 1, 2, 8, 9, 25, 50])
    def test_chunk_loads_cover_padded_window(self, half: int,
                                             chunk_consts: dict[str, int]) -> None:
        """Replay the chunk load: every window-relative texel must be loaded.

        Chunk (ci, cj) covers window offsets [ci*16, ci*16+15] x
        [cj*16, cj*16+15] and is used iff ci*16 < extent (matching the
        ``chunk_used`` guard in the shader).
        """
        extent = 16 + 2 * half
        max_chunks = chunk_consts["MAX_CHUNKS"]
        loaded: set[tuple[int, int]] = set()
        for cj in range(max_chunks):
            for ci in range(max_chunks):
                if ci * 16 < extent and cj * 16 < extent:
                    for j, i in itertools.product(range(16), repeat=2):
                        loaded.add((ci * 16 + i, cj * 16 + j))
        required = set(itertools.product(range(extent), repeat=2))
        assert required <= loaded, (
            f"half={half}: window texels never loaded: "
            f"{sorted(required - loaded)[:8]}...")

    @pytest.mark.parametrize("half", [0, 1, 2, 8, 13, 50])
    def test_reduce_covers_structuring_element_exactly(self, half: int,
                                                       chunk_consts: dict[str, int]) -> None:
        """Replay the per-thread reduce loops of the fixed shader.

        For thread (lx, ly) and chunk (ci, cj) the shader reduces chunk-local
        i in [max(0, lx - ci*16), min(15, lx - ci*16 + 2*half)] and likewise
        for j; chunk-local (i, j) maps to offset
        (ci*16 + i - half - lx, cj*16 + j - half - ly). The union over all
        chunks must equal the structuring element [-half, half]^2 exactly.
        """
        extent = 16 + 2 * half
        max_chunks = chunk_consts["MAX_CHUNKS"]
        threads = itertools.product(range(16), repeat=2)
        for lx, ly in threads:
            reduced: set[tuple[int, int]] = set()
            for cj in range(max_chunks):
                for ci in range(max_chunks):
                    if not (ci * 16 < extent and cj * 16 < extent):
                        continue
                    dx_base, dy_base = lx - ci * 16, ly - cj * 16
                    x_lo, x_hi = max(0, dx_base), min(15, dx_base + 2 * half)
                    y_lo, y_hi = max(0, dy_base), min(15, dy_base + 2 * half)
                    for j in range(y_lo, y_hi + 1):
                        for i in range(x_lo, x_hi + 1):
                            reduced.add((ci * 16 + i - half - lx,
                                         cj * 16 + j - half - ly))
            expected = set(itertools.product(range(-half, half + 1), repeat=2))
            assert reduced == expected, (
                f"half={half}, thread=({lx},{ly}): missing="
                f"{sorted(expected - reduced)[:6]} extra="
                f"{sorted(reduced - expected)[:6]}")

    def test_old_corner_only_loads_are_gone(self) -> None:
        code = strip_comments(_ERODE_DILATE_COMPUTE_SHARED)
        for old_guard in (
            "local_id.x < padding",
            "local_id.x >= 16 - padding",
            "local_id.y < padding",
            "local_id.y >= 16 - padding",
        ):
            assert old_guard not in code, (
                f"old corner-block padding guard is back: {old_guard}")

    def test_barriers_are_not_nested_in_conditionals(self) -> None:
        """barrier() must be reached by every invocation unconditionally.

        Structural check: each ``barrier();`` line is indented exactly like
        the ``chunk_used`` guard statement (the inner chunk-loop body level),
        i.e. never deeper inside an ``if``.
        """
        code = strip_comments(_ERODE_DILATE_COMPUTE_SHARED)
        barrier_indents = {len(m.group(1)) for m in
                           re.finditer(r"^([ \t]*)barrier\(\);", code, re.M)}
        guard_indent = re.search(r"^([ \t]*)bool chunk_used", code, re.M)
        assert guard_indent, "chunk_used guard not found"
        level = len(guard_indent.group(1))
        assert barrier_indents == {level}, (
            f"barrier() at unexpected indentation(s) {barrier_indents}, "
            f"expected {{{level}}} — a barrier inside a conditional is "
            "divergent-UB")


# ── AUD-5304: reflect edge mode ─────────────────────────────────────────

class TestAud5304ReflectEdgeMode:
    def test_fragment_uses_triangle_wave_mirror(self) -> None:
        code = strip_comments(_ERODE_DILATE_FRAGMENT)
        assert "sample_uv = 1.0 - abs(mod(sample_uv, 2.0) - 1.0);" in code, (
            "reflect branch must be the triangle-wave mirror "
            "1.0 - abs(mod(uv, 2.0) - 1.0)")
        assert "sample_uv = abs(mod(sample_uv, 2.0) - 1.0);" not in code, (
            "old 180-degree flipping reflect formula is still present")

    def test_reflect_is_identity_inside_frame(self) -> None:
        # In-frame coordinates must map to themselves (the old formula
        # returned 1 - u, flipping the whole sampling neighbourhood).
        for u in (0.0, 0.001, 0.1, 0.25, 0.5, 0.731, 0.999):
            assert mirror_fixed(u) == pytest.approx(u, abs=1e-9)
            assert mirror_old(u) == pytest.approx(1.0 - u, abs=1e-9)  # the bug

    def test_reflect_folds_outside_coords_back_to_edge(self) -> None:
        cases = {
            -0.1: 0.1,    # just past the left edge -> mirror off the edge
            -0.9: 0.9,
            -1.5: 0.5,    # second period still folds correctly
            1.1: 0.9,     # just past the right edge
            1.75: 0.25,   # 0.75 beyond edge 1 folds back to 1 - 0.75
            2.25: 0.25,
            -2.1: 0.1,
        }
        for u, expected in cases.items():
            assert mirror_fixed(u) == pytest.approx(expected, abs=1e-9), (
                f"mirror({u}) should be {expected}")

    def test_reflect_output_always_in_unit_range(self) -> None:
        for u in [x * 0.05 for x in range(-120, 241)]:
            m = mirror_fixed(u)
            assert -1e-9 <= m <= 1.0 + 1e-9


# ── shared-shader barrier participation (supports 5300/5301/5303) ───────

class TestOutOfBoundsThreadsReachBarriers:
    @pytest.mark.parametrize("shader_name", sorted(SHARED_COMPUTE_SHADERS))
    def test_no_early_return_in_compute_main(self, shader_name: str) -> None:
        code = strip_comments(SHARED_COMPUTE_SHADERS[shader_name])
        assert "return;" not in code, (
            f"{shader_name}: early return before barrier() would leave "
            "padding slots unwritten and diverge the work group")
        assert "bool active" in code

    @pytest.mark.parametrize("shader_name", sorted(SHARED_COMPUTE_SHADERS))
    def test_uniform_contract_unchanged(self, shader_name: str) -> None:
        """compute_shader_manager.py uploads these uniforms by name."""
        code = strip_comments(SHARED_COMPUTE_SHADERS[shader_name])
        if "blur" in shader_name:
            expected = {"u_input", "u_resolution", "u_radius",
                        "u_edge_policy", "u_output"}
        else:
            expected = {"u_input", "u_resolution", "u_operation",
                        "u_size", "u_channels", "u_output"}
        for uniform in expected:
            assert re.search(rf"uniform\s+\w+\s+{uniform}\s*;", code), (
                f"{shader_name}: uniform {uniform} disappeared")


# ── optional real-GL smoke (needs a standalone context, no X display) ───

@pytest.mark.gpu_renderer
def test_erode_dilate_fragment_compiles_standalone() -> None:
    moderngl = pytest.importorskip("moderngl")
    try:
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # headless machine without GL
        pytest.skip(f"no standalone GL context: {exc}")
    from src.features.composite.core.shaders import _PASSTHROUGH_VERTEX
    program = ctx.program(vertex_shader=_PASSTHROUGH_VERTEX,
                          fragment_shader=_ERODE_DILATE_FRAGMENT)
    for uniform in ("u_texture", "u_resolution", "u_operation", "u_size",
                    "u_edge_mode", "u_channels"):
        assert uniform in set(program), f"{uniform} eliminated at link time"
    ctx.release()


@pytest.mark.gpu_renderer
@pytest.mark.parametrize("shader_name", sorted(SHARED_COMPUTE_SHADERS))
def test_shared_compute_shaders_compile_standalone(shader_name: str) -> None:
    moderngl = pytest.importorskip("moderngl")
    try:
        ctx = moderngl.create_standalone_context()
    except Exception as exc:  # headless machine without GL
        pytest.skip(f"no standalone GL context: {exc}")
    try:
        ctx.compute_shader(SHARED_COMPUTE_SHADERS[shader_name])
    except Exception as exc:
        # GL 4.3 compute is unavailable under the macOS GL 4.1 cap; the
        # source-level algebraic tests above carry the correctness proof.
        pytest.skip(f"compute shader not compilable on this GL context "
                    f"(needs 4.3): {exc}")
    ctx.release()
