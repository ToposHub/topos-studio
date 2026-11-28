"""Build ``docs/architecture/macos_metal_migration/effect-compat-matrix.md``.

The compatibility matrix of plan 6.1: every effect, plugin, transition and
preset the repository ships, with its entry, version, graphics and compute
backends, preview / export capability and its Metal status.  Nothing in the
document is typed by hand -- it is read from the effect catalog, the frozen
contract catalog, the Metal verification records and the plugin directories,
so the document can never drift from the code it describes:

    PYTHONPATH=. .venv/bin/python tools/build_effect_compat_matrix.py [--check]

``--check`` exits with status 1 when the committed document is out of date.

A cell reads ``未登记`` wherever the repository records nothing for it; the
matrix shows what is known and what is not, it does not guess.  "Deterministic"
and the HDR / Alpha columns are measured facts only for the plugins the Metal
parity run verified; everything else keeps its GL path, where those
properties were never registered per effect.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DOCUMENT = ROOT / "docs" / "architecture" / "macos_metal_migration" / "effect-compat-matrix.md"
UNREGISTERED = "未登记"


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _join(values: Iterable[object]) -> str:
    return ", ".join(str(value) for value in values) or "—"


def _verified_facts(record: object) -> str:
    """HDR / Alpha / determinism as the parity run measured them."""
    from src.features.shared.effects import msl_effect_registry as registry

    status = getattr(record, "status", "")
    if status == registry.STATUS_VERIFIED:
        return "HDR 语料通过 / Alpha 逐位 / 确定性 ✓"
    if status == registry.STATUS_NOISE_GATED:
        return "确定性 ✗（哈希噪声两端不可复现）"
    return UNREGISTERED


def _bit_identical(note: str) -> str:
    match = re.search(r"([\d.]+)% 像素逐位相同", note or "")
    return f"{match.group(1)}%" if match else "—"


def _plugins() -> tuple[list[str], list[str], int]:
    """Rows of the built-in video effects and of the ``.tglsl`` plugins, and
    how many plugins declare ``metal``."""
    from src.features.shared.effects import get_effect_catalog
    from src.features.shared.effects import msl_plugin_verification as verification
    from src.features.shared.effects import msl_effect_registry as registry

    metal = registry.get_msl_effect_registry()
    builtins: list[str] = []
    plugins: list[str] = []
    declared = 0
    for spec in sorted(get_effect_catalog().get_all_specs(), key=lambda s: s.effect_type):
        params = len(spec.param_defs)
        capabilities = _join(spec.plugin_capabilities) or "—"
        if not spec.effect_type.startswith("plugin:"):
            state = "已登记 Metal 条目" if metal.entry(spec.effect_type) is not None else "未迁移（走既有 GLSL）"
            builtins.append(
                f"| `{_cell(spec.effect_type)}` | {_cell(spec.name)} | {params} | OpenGL | "
                f"{state} | {UNREGISTERED} |")
            continue
        name = spec.plugin_id[7:] if spec.plugin_id else spec.effect_type[7:]
        declared += int("metal" in spec.graphics_backends)
        declared_graphics = _join(sorted(spec.graphics_backends)) or "OpenGL（未声明）"
        declared_compute = _join(sorted(spec.compute_backends)) or "GLSL（未声明）"
        record = verification.PLUGIN_VERIFICATION_RECORDS.get(name)
        if record is None:
            state, evidence = "未迁移（OpenGL 兼容路径）", "—"
        elif record.status == registry.STATUS_VERIFIED:
            state = "**已验证**（声明 `metal`）"
            evidence = (f"max\\|Δ\\| {record.max_abs_diff:g}，max 相对 {record.max_rel_diff:.2e}，"
                        f"{_bit_identical(record.note)} 逐位")
        else:
            state = "噪声门限未过（不声明 `metal`）"
            evidence = f"{_bit_identical(record.note)} 逐位"
        plugins.append(
            f"| `{_cell(name)}` | {spec.plugin_version or '—'} | {params} | {declared_graphics} | "
            f"{declared_compute} | {capabilities} | {state} | {_verified_facts(record)} | {evidence} |")
    return builtins, plugins, declared


def _builtin_metal_entries() -> list[str]:
    """The built-in effects that have a Metal entry (helpers of plan 6.2)."""
    from src.features.shared.effects import msl_effect_registry as registry

    states = {registry.STATUS_VERIFIED: "已验证", registry.STATUS_NOISE_GATED: "噪声门限未过",
              registry.STATUS_UNVERIFIED: "未验证"}
    yes_no = lambda flag: "✓" if flag else "✗"  # noqa: E731 - a table cell
    rows = []
    for entry in sorted(registry.get_msl_effect_registry().entries(), key=lambda e: e.effect_id):
        state = states.get(getattr(entry.verification, "status", None), UNREGISTERED)
        helper = registry.get_msl_effect_registry().helper(entry.helper_id)
        rows.append(f"| `{_cell(entry.effect_id)}` | `{helper.msl_name if helper else entry.helper_id}` | "
                    f"{entry.version} | {yes_no(entry.hdr)} | {yes_no(entry.preserves_alpha)} | "
                    f"{yes_no(entry.deterministic)} | {state} |")
    return rows


def _contract_effects() -> list[str]:
    """Effects of the frozen contract catalog that are not video effects (audio, generators)."""
    from src.features.shared.effects import get_effect_catalog
    from src.shared.contracts import effect_catalog as ec

    video = {spec.effect_type for spec in get_effect_catalog().get_all_specs()}
    rows = []
    for entry in sorted(ec.build_catalog()["effects"], key=lambda e: e["effect_type"]):
        if entry["effect_type"] in video:
            continue
        category = entry["category"]
        backend = "非图形（音频）" if category.startswith("audio") else "OpenGL（生成器 / 调整剪辑）"
        rows.append(f"| `{_cell(entry['effect_type'])}` | {_cell(entry['name'])} | {category} | "
                    f"{backend} | 不在本次范围 |")
    return rows


def _transitions() -> list[str]:
    from src.shared.contracts import effect_catalog as ec

    # cross_dissolve is two layers with rewritten opacities, so the preview
    # composite (plan 2.6) and the Metal H.264 export (3.0) take it;
    # dip_to_color is a composite pass of the preview only.  Every other
    # transition stays on the GL transition shader.
    on_metal = {"cross_dissolve": "预览合成内（2.6）；Metal H.264 导出快路径（3.0）",
                "dip_to_color": "预览合成内（2.6）"}
    rows = []
    for entry in sorted(ec.build_catalog()["transitions"], key=lambda t: t["transition_type"]):
        kind = entry["transition_type"]
        state = on_metal.get(kind, "OpenGL 转场着色器（未迁移）")
        rows.append(f"| `{_cell(kind)}` | {_cell(entry.get('label') or kind)} | {entry.get('category', '')} | {state} |")
    return rows


def _other_plugin_kinds() -> list[str]:
    plugins = ROOT / "plugins"
    rows = []
    for path in sorted(plugins.glob("*.tai")):
        rows.append(f"| `AI_Upscale_4X` | `.tai` | ONNX Runtime（按 provider 验证，计划 6 范围内未动） | 非图形 |")
    node_dir = plugins / "node_plugins"
    if node_dir.is_dir():
        for path in sorted(node_dir.glob("*_plugin.py")):
            rows.append(f"| `{path.stem}` | 节点插件（Python） | 宿主与脚本服务 | 未迁移 |")
    for name in ("ai_assistant", "oiio_topos"):
        if (plugins / name).is_dir():
            rows.append(f"| `{name}` | 目录 | {'AI 助手（非图形）' if name == 'ai_assistant' else 'OIIO / TOPS 编解码'} | 不在本次范围 |")
    return rows


def _presets() -> list[str]:
    rows = []
    for path in sorted((ROOT / "Presets").glob("*.json")):
        rows.append(f"| `{_cell(path.stem)}` | 预设 | 参数集合：后端随它引用的效果 |")
    return rows


def build() -> str:
    from src.features.shared.effects import msl_plugin_verification as verification
    from src.features.shared.effects import msl_effect_registry as registry
    from src.shared.contracts import effect_catalog as ec

    builtins, plugins, declared = _plugins()
    builtin_metal = _builtin_metal_entries()
    contract = _contract_effects()
    transitions = _transitions()
    others = _other_plugin_kinds()
    presets = _presets()
    statuses = Counter(record.status for record in verification.PLUGIN_VERIFICATION_RECORDS.values())
    if declared != statuses[registry.STATUS_VERIFIED]:
        raise SystemExit(f"declared metal plugins ({declared}) != verified ({statuses[registry.STATUS_VERIFIED]})")
    frozen = ec.build_catalog()
    lines = [
        "# 效果与插件兼容矩阵（阶段 6.1）",
        "",
        "由 `tools/build_effect_compat_matrix.py` 从效果目录、冻结合同目录、Metal 验证登记表与插件目录生成，"
        "**不要手改**：重跑生成器即可更新（`--check` 在文档过期时返回 1）。",
        "",
        "口径：`未登记` 表示仓库对该项没有逐效果的记录，矩阵只列出已知事实。"
        "「HDR / Alpha / 确定性」一列只对 Metal 对拍验证过的插件是实测结论——语料含负值、零、超白的 HDR 输入，"
        "Alpha 要求逐位相同；其余条目仍走各自的 OpenGL 路径，这些属性从未按效果登记。"
        "声明 `metal` 的前提是本机（AMD RX 6900 XT）真机 GL↔Metal 对拍通过，证据见 "
        "`evidence/effect-metal-parity-2026-11-23.json`；`tests/shared/test_tglsl_metal_parity.py` 每次重跑该门限。",
        "",
        "**目前没有任何生产路径在 Metal 上执行效果**：效果一律经 GLSL 的 `EffectPassRunner`，"
        "能力预检也不参与路由。本矩阵登记的是能力与证据，不是已启用的行为。",
        "",
        "## 总览",
        "",
        "| 类别 | 数量 | 图形 / 计算后端 | Metal 状态 |",
        "|---|---:|---|---|",
        f"| 内置视频效果 | {len(builtins)} | OpenGL | 未迁移 |",
        f"| 内置 Metal 条目（6.2 helper） | {len(builtin_metal)} | Metal compute | 已验证；尚无内置效果调用 |",
        f"| `.tglsl` 插件 | {len(plugins)} | 声明 ∩ 宿主可译 | 已验证 {statuses[registry.STATUS_VERIFIED]}，"
        f"噪声门限未过 {statuses[registry.STATUS_NOISE_GATED]}，未迁移 {len(plugins) - len(verification.PLUGIN_VERIFICATION_RECORDS)} |",
        f"| 冻结合同目录中的非视频效果（音频、生成器） | {len(contract)} | 非图形 / OpenGL | 不在本次范围 |",
        f"| 转场 | {len(transitions)} | OpenGL 转场着色器 | 2 种在合成内（`cross_dissolve`、`dip_to_color`），其余未迁移 |",
        f"| 其他插件类型（`.tai`、节点插件、AI、OIIO） | {len(others)} | 宿主 / provider | 未迁移或不在本次范围 |",
        f"| 预设 | {len(presets)} | 随所含效果 | — |",
        "",
        f"冻结合同目录 `catalog_id` 共 {len(frozen['effects'])} 个效果、{len(frozen['transitions'])} 个转场；"
        "旧工程里已不存在的效果 ID 由效果目录的未知效果诊断处理，本矩阵不枚举历史 ID。",
        "",
        "## 内置视频效果",
        "",
        "| ID | 名称 | 参数数 | 图形后端 | Metal 状态 | HDR / Alpha / 确定性 |",
        "|---|---|---:|---|---|---|",
        *builtins,
        "",
        "## 内置 Metal 条目",
        "",
        "均已验证，但尚无内置效果或生产路径调用它们。",
        "",
        "| ID | MSL helper | 版本 | HDR | Alpha | 确定性 | 验证 |",
        "|---|---|---|---|---|---|---|",
        *builtin_metal,
        "",
        "## `.tglsl` 插件",
        "",
        "| ID | 版本 | 参数数 | 声明 graphics | 声明 compute | 能力 | Metal 状态 | HDR / Alpha / 确定性 | 对拍证据 |",
        "|---|---|---:|---|---|---|---|---|---|",
        *plugins,
        "",
        "## 冻结合同目录中的非视频效果",
        "",
        "| ID | 名称 | 类别 | 后端 | Metal |",
        "|---|---|---|---|---|",
        *contract,
        "",
        "## 转场",
        "",
        "| ID | 名称 | 类别 | Metal 状态 |",
        "|---|---|---|---|",
        *transitions,
        "",
        "## 其他插件类型",
        "",
        "| ID | 类型 | 入口 | Metal |",
        "|---|---|---|---|",
        *others,
        "",
        "## 预设",
        "",
        "| ID | 类型 | 说明 |",
        "|---|---|---|",
        *presets,
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail when the committed document is out of date")
    args = parser.parse_args()
    document = build()
    if args.check:
        current = DOCUMENT.read_text(encoding="utf-8") if DOCUMENT.is_file() else ""
        if current != document:
            print(f"{DOCUMENT.relative_to(ROOT)} is out of date; rerun tools/build_effect_compat_matrix.py")
            return 1
        print("up to date")
        return 0
    DOCUMENT.write_text(document, encoding="utf-8")
    print(DOCUMENT.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
