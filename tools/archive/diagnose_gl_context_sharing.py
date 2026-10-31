#!/usr/bin/env python3
"""GL Context Sharing Diagnostic Tool.

Outputs:
- AA_ShareOpenGLContexts status
- Context format (version, profile) for each widget
- 2x2 probe result (pass/fail)
- Direct texture presentation recommendation

Usage:
    .venv/bin/python tools/diagnose_gl_context_sharing.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget

    QApplication.setAttribute(
        Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True
    )
    app = QApplication(sys.argv)

    print("=== GL Context Sharing Diagnostic ===")
    print("AA_ShareOpenGLContexts: enabled")

    # Create two QOpenGLWidget instances
    w1 = QOpenGLWidget()
    w2 = QOpenGLWidget()
    w1.show()
    w1.hide()
    w2.show()
    w2.hide()

    ctx1 = w1.context()
    ctx2 = w2.context()

    fmt1 = ctx1.format()
    fmt2 = ctx2.format()
    print(
        f"Widget1: version={fmt1.majorVersion()}.{fmt1.minorVersion()}, "
        f"profile={fmt1.profile()}"
    )
    print(
        f"Widget2: version={fmt2.majorVersion()}.{fmt2.minorVersion()}, "
        f"profile={fmt2.profile()}"
    )

    # 2x2 pixel probe
    try:
        import moderngl

        w1.makeCurrent()
        mgl1 = moderngl.create_context()
        w2.makeCurrent()
        mgl2 = moderngl.create_context()

        # Write test texture in ctx1
        w1.makeCurrent()
        tex = mgl1.texture(
            size=(2, 2),
            components=4,
            dtype="f1",
            data=(
                b"\xff\x00\x00\xff\x00\xff\x00\xff"
                b"\x00\x00\xff\xff\xff\xff\xff\xff"
            ),
        )

        # Read from ctx2
        w2.makeCurrent()
        import ctypes
        from OpenGL import GL as _gl

        w_val = ctypes.c_int()
        _gl.glBindTexture(_gl.GL_TEXTURE_2D, tex.glo)
        _gl.glGetTexLevelParameteriv(
            _gl.GL_TEXTURE_2D, 0, _gl.GL_TEXTURE_WIDTH, ctypes.byref(w_val)
        )
        _gl.glBindTexture(_gl.GL_TEXTURE_2D, 0)
        result = w_val.value == 2
        print(f"2x2 probe: {'PASS' if result else 'FAIL'}")
        print(
            f"Direct texture presentation: {'enabled' if result else 'disabled'}"
        )
    except Exception as e:
        print(f"2x2 probe: ERROR ({e})")
        print("Direct texture presentation: disabled")

    print("=== Done ===")
    app.quit()


if __name__ == "__main__":
    main()
