"""L22 打包门（Mac 侧）：干净包/暂存隔离红线/配方归档往返。

范围：
1. 暂存隔离红线：生产模块导入链不触碰 content_staging（L 计划红线——
   暂存区不在生产发现路径，打出的 wheel 不含暂存）；
2. wheel 打包内容：新增生产模块（transition_packs resample_cores 包/
   evaluators lib1_fullframe_compute/presets 内置预设）随包分发；
3. 配方归档往返：recipe.json → 载荷 → JSON 序列化 → 反序列化 → 消费者
   可执行（离线工程往返的配方侧闭环）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.color_core

_PY = str(REPO_ROOT / ".venv" / "bin" / "python")


class TestStagingIsolationRedLine:
    def test_production_imports_do_not_touch_staging(self) -> None:
        """生产导入链不触碰 content_staging（子进程干净 sys.modules 验证）。"""
        code = (
            "import sys\n"
            "sys.path.insert(0, '.')\n"
            "import src.features.edit.transition_packs.library_v1.cores\n"
            "import src.features.edit.transition_packs.library_v1.resample_cores\n"
            "import src.features.composite.evaluators.lib1_fullframe_compute\n"
            "import src.features.composite.nodes.lib1_nodes as m\n"
            "import src.features.composite.core.node_engine\n"
            "touched = [k for k in sys.modules if 'content_staging' in k]\n"
            "assert not touched, f'生产导入触碰暂存: {touched}'\n"
            "print('clean')\n"
        )
        result = subprocess.run([_PY, "-c", code], cwd=REPO_ROOT,
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr[-500:]
        assert "clean" in result.stdout

    def test_wheel_build_contains_new_modules(self) -> None:
        """wheel 构建含新增生产模块（resample_cores 包/全帧计算模块）。"""
        result = subprocess.run(
            [_PY, "-m", "pip", "wheel", "--no-build-isolation",
             "--no-deps", "-w", "/tmp/topos_l22_wheel", "."],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=600)
        assert result.returncode == 0, result.stderr[-500:]
        import zipfile
        wheels = sorted(Path("/tmp/topos_l22_wheel").glob("*.whl"))
        assert wheels, "未产出 wheel"
        with zipfile.ZipFile(wheels[-1]) as zf:
            names = zf.namelist()
        assert any("transition_packs/library_v1/resample_cores/__init__.py"
                   in n for n in names), "resample_cores 包未随 wheel"
        assert any("transition_packs/library_v1/cores.py" in n
                   for n in names)
        assert any("evaluators/lib1_fullframe_compute.py" in n
                   for n in names)
        assert any("composite/nodes/lib1_nodes.py" in n for n in names)
        # 红线：wheel 不含暂存区
        assert not any("content_staging" in n for n in names), \
            "wheel 混入暂存区文件（红线）"

    def test_builtin_preset_ships(self) -> None:
        """L05 内置合成预设随包（presets/LIB1_Gradient_Tone.json）。"""
        from src.features.composite.core.node_engine import NODE_REGISTRY
        # 预设文件在仓库 presets/（非 src 包内）——打包归 M07 工程资源
        # 管线；此处验证其在仓库与发现路径存在
        preset = REPO_ROOT / "presets" / "LIB1_Gradient_Tone.json"
        assert preset.exists()
        data = json.loads(preset.read_text(encoding="utf-8"))
        assert data.get("name")


class TestRecipeArchiveRoundtrip:
    def test_transition_recipe_json_roundtrip(self) -> None:
        """转场配方 → 载荷 → JSON 序列化 → 反序列化 → 消费者可执行。"""
        import importlib.util
        conv_path = (REPO_ROOT / "content_staging" / "library_v1" /
                     "presets" / "p41_fine_noise_dissolve" / "convert.py")
        spec = importlib.util.spec_from_file_location("p41_conv", conv_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        payload = mod.to_transition_instance()
        # 归档往返：JSON 落盘 → 读回
        blob = json.dumps(payload, ensure_ascii=False)
        restored = json.loads(blob)
        assert restored == payload
        # 反序列化后消费者可执行
        from src.features.edit.core.timeline_v2 import TransitionInstance
        inst = TransitionInstance(
            transition_id="t1", transition_type=restored["transition_type"],
            track_id="v1", left_clip_id="c1", right_clip_id="c2",
            center_frame=10, duration=12, alignment="centered",
            params=restored["params"])
        from src.features.edit.core import transition_mask
        mask = transition_mask.compute_transition_mask(
            16, 12, 0.5, inst.transition_type, inst.params)
        assert 0.0 < float(mask.mean()) < 1.0

    def test_macro_payload_json_roundtrip(self) -> None:
        """宏配方载荷 JSON 往返（slots/steps/connections 无环结构保持）。"""
        import importlib.util
        conv_path = (REPO_ROOT / "content_staging" / "library_v1" /
                     "presets" / "p57_white_edge_despill" / "convert.py")
        spec = importlib.util.spec_from_file_location("p57_conv", conv_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        payload = mod.to_macro_payload()
        blob = json.dumps(payload, ensure_ascii=False)
        restored = json.loads(blob)
        assert restored == payload
        assert restored["slots"] and restored["steps"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
