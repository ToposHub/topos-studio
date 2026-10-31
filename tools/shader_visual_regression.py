#!/usr/bin/env python3
"""Shader 采样数优化的视觉回归：旧版(git HEAD) vs 新版(工作区)。

同一真实视频帧分别用新旧 BLUR_SHARPEN_SNIPPET / 磨皮插件渲染，
计算 PSNR 并输出 diff 图。用法:
    .venv/bin/python tools/shader_visual_regression.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import av
import moderngl


def git_head_file(relpath: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "show", f"HEAD:{relpath}"],
        capture_output=True, text=True, check=True)
    return out.stdout


def load_snippet_from_source(source: str, name: str) -> str:
    ns: dict = {}
    exec(compile(source, name, "exec"), ns)  # noqa: S102 - trusted local source
    return ns[name]


def decode_first_frame(path: Path, max_w: int = 1920):
    container = av.open(str(path))
    for frame in container.decode(container.streams.video[0]):
        if frame.format.name != "rgb24":
            frame = frame.reformat(format="rgb24", width=max_w)
        arr = np.frombuffer(frame.planes[0], dtype=np.uint8)
        stride = frame.planes[0].line_size
        arr = arr.reshape(frame.height, stride)[:, :frame.width * 3].copy()
        container.close()
        return np.ascontiguousarray(arr.reshape(frame.height, frame.width, 3))
    container.close()
    raise RuntimeError("no frame decoded")


def build_program(ctx, snippet, main_body):
    vs = """
    #version 330
    in vec2 in_vert; in vec2 in_texcoord; out vec2 v_texcoord;
    void main() { gl_Position = vec4(in_vert, 0.0, 1.0); v_texcoord = in_texcoord; }
    """
    fs = f"""
    #version 330
    in vec2 v_texcoord; out vec4 f_color;
    uniform sampler2D u_tex; uniform vec2 u_tex_size;
    {snippet}
    void main() {{
        vec3 c = texture(u_tex, v_texcoord).rgb;
        f_color = vec4({main_body}, 1.0);
    }}
    """
    return ctx.program(vertex_shader=vs, fragment_shader=fs)


def render(ctx, prog, tex, quad, w, h):
    fbo = ctx.framebuffer(ctx.texture((w, h), 3))
    fbo.use()
    ctx.viewport = (0, 0, w, h)
    tex.use(0)
    prog['u_tex'].value = 0
    prog['u_tex_size'].value = (float(w), float(h))
    quad.render(prog)
    data = fbo.read(components=3)
    fbo.color_attachments[0].release()
    fbo.release()
    return np.frombuffer(data, dtype=np.uint8).reshape(h, w, 3)


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    if mse <= 1e-12:
        return float('inf')
    return 10.0 * np.log10(255.0 ** 2 / mse)


def main() -> int:
    import moderngl as mgl
    video = ROOT / "tests/test_videos/2K_h264.mp4"
    frame = decode_first_frame(video)
    h, w = frame.shape[:2]
    print(f"input frame: {w}x{h} from {video.name}")

    old_nodes = git_head_file("src/features/color/core/shader_nodes.py")
    new_nodes = (ROOT / "src/features/color/core/shader_nodes.py").read_text()
    old_skin = git_head_file("plugins/bilateral_skin_smooth.tglsl")
    new_skin = (ROOT / "plugins/bilateral_skin_smooth.tglsl").read_text()

    def skin_body(tglsl_src: str) -> str:
        # strip metadata block, inject uniforms for plugin params
        body = tglsl_src.split("-----------------------------", 1)[1]
        body = body.replace("process(", "skin_process(")
        header = """
        uniform float strength; uniform float radius;
        uniform float threshold; uniform float recover;
        #define u_input_tex u_tex
        """
        return header + body

    cases = []
    # (label, main-body expr, params-common)
    for label, old_src, new_src, call in [
        ("bilateral(r10,detail.5)",
         load_snippet_from_source(old_nodes, "BLUR_SHARPEN_SNIPPET"),
         load_snippet_from_source(new_nodes, "BLUR_SHARPEN_SNIPPET"),
         "bilateral_blur(u_tex, v_texcoord, 10.0, 50.0, u_tex_size)"),
        ("bilateral(r30,detail.5)",
         load_snippet_from_source(old_nodes, "BLUR_SHARPEN_SNIPPET"),
         load_snippet_from_source(new_nodes, "BLUR_SHARPEN_SNIPPET"),
         "bilateral_blur(u_tex, v_texcoord, 30.0, 50.0, u_tex_size)"),
        ("bokeh(r10)",
         load_snippet_from_source(old_nodes, "BLUR_SHARPEN_SNIPPET"),
         load_snippet_from_source(new_nodes, "BLUR_SHARPEN_SNIPPET"),
         "bokeh_blur(u_tex, v_texcoord, 10.0, 50.0, u_tex_size)"),
        ("fast_blur(i.8,r10)",
         load_snippet_from_source(old_nodes, "BLUR_SHARPEN_SNIPPET"),
         load_snippet_from_source(new_nodes, "BLUR_SHARPEN_SNIPPET"),
         "fast_blur(u_tex, v_texcoord, 0.8, 10.0, u_tex_size)"),
        ("USM(mode1,r5,it4)",
         load_snippet_from_source(old_nodes, "BLUR_SHARPEN_SNIPPET"),
         load_snippet_from_source(new_nodes, "BLUR_SHARPEN_SNIPPET"),
         "apply_blur_sharpen(c, u_tex, v_texcoord, 1, 0.5, 0.2, 0.1, 5.0, 4, u_tex_size, true, false, 0, 0.0, 0.0, 0.0, 0.0, 0.0)"),
        ("skin_smooth(s.8,t.1,rec.4)",
         skin_body(old_skin), skin_body(new_skin),
         "skin_process(c, v_texcoord)"),
    ]:
        cases.append((label, old_src, new_src, call))

    ctx = mgl.create_standalone_context()
    tex = ctx.texture((w, h), 3, frame.tobytes())
    tex.filter = (mgl.LINEAR, mgl.LINEAR)
    quad = ctx.vertex_array(
        ctx.program(vertex_shader="""
            #version 330
            in vec2 in_vert; void main() { gl_Position = vec4(in_vert, 0.0, 1.0); }
            """, fragment_shader="""
            #version 330
            void main() {}
            """), [])  # placeholder; real quads built per program below
    quad.release()

    verts = np.array([-1, -1, 1, -1, -1, 1, 1, 1], dtype='f4')
    quad_vao = None

    print(f"{'case':<26} {'PSNR old-vs-new':>16}  mean|diff|  verdict")
    for label, old_src, new_src, call in cases:
        prog_old = build_program(ctx, old_src, call)
        prog_new = build_program(ctx, new_src, call)
        vao_old = ctx.vertex_array(prog_old, [(ctx.buffer(verts.tobytes()), '2f', 'in_vert')])
        # need uv too
        vao_old.release()
        # rebuild with uv
        quad_data = np.array([-1, -1, 0, 0, 1, -1, 1, 0, -1, 1, 0, 1, 1, 1, 1, 1], dtype='f4')
        vbo = ctx.buffer(quad_data.tobytes())

        def draw(prog):
            vao = ctx.vertex_array(prog, [(vbo, '2f 2f', 'in_vert', 'in_texcoord')])
            fbo = ctx.simple_framebuffer((w, h))
            fbo.use()
            ctx.viewport = (0, 0, w, h)
            tex.use(0)
            prog['u_tex'].value = 0
            if 'u_tex_size' in prog._members:
                prog['u_tex_size'].value = (float(w), float(h))
            vao.render(mgl.TRIANGLE_STRIP)
            data = np.frombuffer(fbo.read(components=3), dtype=np.uint8).reshape(h, w, 3)
            vao.release()
            fbo.release()
            return data

        out_old = draw(prog_old)
        out_new = draw(prog_new)
        p = psnr(out_old, out_new)
        meandiff = float(np.mean(np.abs(out_old.astype(int) - out_new.astype(int))))
        verdict = "OK" if p >= 30.0 else ("SOFT" if p >= 25.0 else "CHECK")
        print(f"{label:<26} {p:>10.2f} dB   {meandiff:8.3f}  {verdict}")
        prog_old.release()
        prog_new.release()
        vbo.release()

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
