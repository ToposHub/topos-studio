"""C0.2 skip 审计（计划表 B · Windows 线，docs/perf_plan_B_windows_2026-10-22.md）。

对 tests/ 做纯 AST 静态归类（不导入任何测试模块）：

- `pytest.skip(...)`（区分模块级/函数内）、`@pytest.mark.skipif`、
  `@pytest.mark.skip`、`pytest.importorskip(...)`、`pytest.param(..., marks=skip)`
- 按"条件 + 理由"文本归类：OS 门（win32/darwin/linux）、GPU/硬件适配器、
  真实素材、外设（DeckLink/VLC/摄像头）、可选依赖、CI/环境变量、slow/perf、
  wip/not-yet、其余待调查。

静态 skip 语句数与运行时 skip 用例数是两个口径：本脚本产出前者；
后者由 C0.1 的 --junitxml 报告统计（运行时跳过还包含条件在采集机器上
实际为真/假的动态判定）。两者对拍见 docs/test_skip_audit_windows_2026-10.md。

用法：
    .venv/Scripts/python.exe scripts/audit_test_skips.py [--root tests] [--json out.json]
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 归类规则：按顺序首个命中即归类；(子串族, 类别名)。
RULES: list[tuple[tuple[str, ...], str]] = [
    (("win32", "windows", "platform.system() == 'windows'"), "os_windows"),
    (("darwin", "macos", "mac ", "pyobjc", "metal", "videotoolbox",
      "iosurface", "mtl"), "os_macos"),
    (("linux", "posix", "dmabuf"), "os_linux"),
    (("nvidia", "geforce", "radeon", "amd", "nvdec", "nvenc", "d3d11", "d3d12",
      "wgl", "amf", "qsv", "vulkan", "gl_interop", "has_gpu",
      "hardware_capability", "adapter"), "gpu_hardware"),
    (("gl context", "moderngl", "opengl", "standalone gl", "gl_"), "gl_context"),
    (("libtopos", "topos_codec", "dylib", "hwdecode_windows"), "topos_native_lib"),
    (("test_videos", "real_media", "media asset", "素材", "topos_movie",
      "codec_available", "not available", "missing", "no test videos",
      "no test video", "test material", "fixture", "not enough frames",
      "duration unknown", "video modules", "no gop codec"),
     "media_fixture_missing"),
    (("decklink", "bmd", "blackmagic", "vlc", "camera", "webcam", "monitor_output",
      "外设"), "external_device"),
    (("importorskip",), "optional_dep_importorskip"),
    (("pyqt", "qt6", "has_qt", "qapplication"), "qt_dep"),
    (("onnxruntime", "whisper", "rawpy", "cv2", "opencv", "ocio", "opencolorio",
      "pyav", "lut exporter", "tracking fixture"), "optional_dep_named"),
    (("not installed", "unavailable", "不可用", "未构建", "未安装", "api key",
      "browser", "ffmpeg", "numpy", "deepseek", "no audio decoded",
      "no param_defs"), "optional_tool_or_api"),
    (("getenv", "environ", "ci", "github_actions", "gating", "env gate"),
     "env_ci_gate"),
    (("slow", "benchmark", "perf", "long_run", "soak", "stress"), "slow_perf"),
    (("not_yet", "not yet", "todo", "wip", "unimplemented", "placeholder",
      "skeleton", "骨架", "未实现", "待实现"), "wip_not_yet"),
    (("flaky", "unstable", "known failure", "xfail"), "known_unstable"),
    (("not 10-bit", "is not", "not enough", "requires full", "screenshot",
      "font", "dropdown"), "content_or_ui_conditional"),
]


def classify(*texts: str) -> str:
    blob = " ".join(t.lower() for t in texts if t)
    for needles, cat in RULES:
        if any(n in blob for n in needles):
            return cat
    return "unclassified"


def _literal(node: ast.AST) -> str:
    """提取 skip 调用里的 reason/条件字面量；非常量表达式给截断源码。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    try:
        return ast.unparse(node)[:160]
    except Exception:
        return "<dynamic>"


def _marker_args(node: ast.Call) -> tuple[str, str]:
    """(condition, reason) 从 pytest.mark.skipif/skip 调用参数中取。"""
    cond, reason = "", ""
    if node.args:
        cond = _literal(node.args[0])
    for kw in node.keywords:
        if kw.arg == "reason":
            reason = _literal(kw.value)
        elif kw.arg is None:
            pass
    return cond, reason


def _enclosing_lineno(tree: ast.Module, lineno: int) -> int:
    return lineno


def audit_file(path: Path) -> list[dict]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError as exc:
        return [{"file": str(path), "kind": "parse_error", "line": exc.lineno or 0,
                 "category": "parse_error", "detail": str(exc)}]
    entries: list[dict] = []
    rel = str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        name = func.attr
        if name == "skip" and isinstance(func.value, ast.Name) \
                and func.value.id == "pytest":
            reason = _literal(node.args[0]) if node.args else ""
            module_level = any(
                isinstance(mod_node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef))
                for mod_node in ast.walk(tree)
                if getattr(mod_node, "lineno", 0) <= node.lineno <=
                getattr(mod_node, "end_lineno", 0)
                and _contains(mod_node, node)
            )
            entries.append({
                "file": rel, "kind": "pytest.skip", "line": node.lineno,
                "scope": "in_callable" if module_level else "module_level",
                "category": classify(reason),
                "detail": reason,
            })
        elif name == "importorskip" and isinstance(func.value, ast.Name) \
                and func.value.id == "pytest":
            mod = _literal(node.args[0]) if node.args else ""
            entries.append({
                "file": rel, "kind": "importorskip", "line": node.lineno,
                "scope": "in_callable",
                "category": classify("importorskip", mod),
                "detail": mod,
            })
        elif name == "skipif" and isinstance(func.value, ast.Attribute) \
                and func.value.attr == "mark":
            cond, reason = _marker_args(node)
            entries.append({
                "file": rel, "kind": "mark.skipif", "line": node.lineno,
                "scope": "decorator",
                "category": classify(cond, reason),
                "detail": f"cond={cond} reason={reason}",
            })
        elif name == "skip" and isinstance(func.value, ast.Attribute) \
                and func.value.attr == "mark":
            cond, reason = _marker_args(node)
            entries.append({
                "file": rel, "kind": "mark.skip", "line": node.lineno,
                "scope": "decorator",
                "category": classify(reason),
                "detail": f"reason={reason or cond}",
            })

    # 直接标记语法：@skip / @skipIf / @unittest.skip（unittest 风格）
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                target = None
                if isinstance(dec, ast.Name) and dec.id in ("skip", "skipIf"):
                    target = dec.id
                elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) \
                        and dec.func.id in ("skip", "skipIf"):
                    target = dec.func.id
                if target:
                    detail = ""
                    if isinstance(dec, ast.Call):
                        detail = ", ".join(_literal(a) for a in dec.args)
                    entries.append({
                        "file": rel, "kind": f"unittest.{target}", "line": dec.lineno,
                        "scope": "decorator",
                        "category": classify(detail),
                        "detail": detail,
                    })
    return entries


def _contains(parent: ast.AST, child: ast.AST) -> bool:
    return any(n is child for n in ast.walk(parent))


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="tests")
    parser.add_argument("--json", default=None, help="JSON 输出路径（缺省 scripts/test_skip_audit.json）")
    args = parser.parse_args()

    root = PROJECT_ROOT / args.root
    all_entries: list[dict] = []
    files_scanned = 0
    for py in sorted(root.rglob("*.py")):
        files_scanned += 1
        all_entries.extend(audit_file(py))

    by_cat: Counter[str] = Counter()
    by_kind: Counter[str] = Counter()
    by_cat_files: defaultdict[str, set] = defaultdict(set)
    for e in all_entries:
        by_cat[e["category"]] += 1
        by_kind[e["kind"]] += 1
        by_cat_files[e["category"]].add(e["file"])

    report = {
        "schema": "test_skip_audit/1",
        "date": datetime.now().isoformat(timespec="seconds"),
        "root": str(args.root),
        "files_scanned": files_scanned,
        "static_skip_statements": len(all_entries),
        "by_kind": dict(by_kind),
        "by_category": dict(by_cat.most_common()),
        "distinct_files_by_category": {k: len(v) for k, v in by_cat_files.items()},
        "entries": all_entries,
    }
    out = Path(args.json) if args.json else PROJECT_ROOT / "scripts" / "test_skip_audit.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"扫描 {files_scanned} 个文件；静态 skip 语句 {len(all_entries)} 条")
    print("\n按类别：")
    for cat, n in by_cat.most_common():
        print(f"  {cat:28s} {n:5d}  ({len(by_cat_files[cat])} 个文件)")
    print("\n按形式：")
    for kind, n in by_kind.most_common():
        print(f"  {kind:28s} {n:5d}")
    unclassified = [e for e in all_entries if e["category"] == "unclassified"]
    if unclassified:
        print(f"\nunclassified {len(unclassified)} 条（待调查，见 JSON entries）")
    print(f"\n结果已保存: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
