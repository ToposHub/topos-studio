"""
Phase 4: LUT Exporter vs GPU Shader consistency tests.

Tests that CPU LUT exporter produces results consistent with GPU shader
for SDR, HDR, and negative inputs.

Requires GPU/moderngl - skipped if unavailable.
"""

import pytest
import numpy as np
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

try:
    import moderngl
    HAS_GPU = True
except ImportError:
    HAS_GPU = False

try:
    from src.features.color.core.lut_exporter import LutExporter as LUTExporter, LutExportWorker
    HAS_EXPORTER = True
except ImportError:
    HAS_EXPORTER = False


def _apply_grading_cpu_reference(color: np.ndarray, params: dict) -> np.ndarray:
    """Reference implementation of apply_grading matching shader_nodes.py."""
    result = color.copy()
    
    exposure = params.get('exposure', 0.0)
    if exposure != 0.0:
        exp_stops = (exposure / 100.0) ** 2 * 6.0
        if exposure < 0.0:
            exp_stops = -exp_stops
        result *= 2.0 ** exp_stops
    
    temp = params.get('temp', 0.0)
    tint = params.get('tint', 0.0)
    wb = np.array([
        1.0 + temp / 200.0,
        1.0 - tint / 200.0,
        1.0 - temp / 200.0
    ])
    result *= wb
    
    contrast = params.get('contrast', 0.0)
    if contrast != 0.0:
        c_factor = 1.0 + contrast / 100.0
        result = np.sign(result) * np.power(np.abs(result) / 0.18, c_factor) * 0.18
    
    lift = np.array(params.get('lift', [1.0, 1.0, 1.0]))
    gain = np.array(params.get('gain', [1.0, 1.0, 1.0]))
    gamma = np.array(params.get('gamma', [1.0, 1.0, 1.0]))
    
    l = (lift - 1.0) * 0.2
    result = result * gain * (1.0 - l) + l
    
    safe_gamma = np.maximum(gamma, 0.01)
    gm = 2.0 ** (-np.log2(safe_gamma))
    
    for i in range(3):
        x = result[:, i] if result.ndim > 1 else result[i]
        if isinstance(x, np.ndarray):
            res_i = np.zeros_like(x)
            mask_sdr = (x > 0.0) & (x <= 1.0)
            mask_hdr = x > 1.0
            mask_neg = x <= 0.0
            
            res_i[mask_sdr] = np.power(x[mask_sdr], gm[i])
            res_i[mask_hdr] = gm[i] * (x[mask_hdr] - 1.0) + 1.0
            res_i[mask_neg] = gm[i] * x[mask_neg]
            
            if result.ndim > 1:
                result[:, i] = res_i
            else:
                result[i] = res_i
        else:
            if x > 0.0 and x <= 1.0:
                if result.ndim > 1:
                    result[:, i] = np.power(result[:, i], gm[i])
                else:
                    result[i] = np.power(x, gm[i])
            elif x > 1.0:
                if result.ndim > 1:
                    result[:, i] = gm[i] * (result[:, i] - 1.0) + 1.0
                else:
                    result[i] = gm[i] * (x - 1.0) + 1.0
            else:
                if result.ndim > 1:
                    result[:, i] = gm[i] * result[:, i]
                else:
                    result[i] = gm[i] * x
    
    return result


def _apply_soft_clip_cpu_reference(color: np.ndarray, params: dict) -> np.ndarray:
    """Reference implementation of apply_soft_clip matching shader_nodes.py."""
    result = color.copy()
    
    exposure = params.get('exposure', 0.0)
    if exposure != 0.0:
        result *= 2.0 ** exposure
    
    contrast = params.get('contrast', 0.0)
    if contrast != 0.0:
        factor = (1.0 + contrast) if contrast >= 0.0 else (1.0 / (1.0 + abs(contrast)))
        result = np.sign(result) * np.power(np.abs(result) / 0.18, factor) * 0.18
    
    high = params.get('high', 0.9)
    white_point = params.get('white_point', 1.0)
    shoulder_strength = params.get('shoulder_strength', 0.5)
    low = params.get('low', 0.1)
    black_point = params.get('black_point', 0.0)
    toe_strength = params.get('toe_strength', 0.5)
    
    for i in range(3):
        c = result[:, i] if result.ndim > 1 else result[i]
        
        if isinstance(c, np.ndarray):
            mask_toe = c < low
            mask_hard_neg = c < black_point
            
            c[mask_hard_neg] = black_point
            
            c_toe = c[mask_toe]
            if len(c_toe) > 0:
                T = low
                B = black_point
                k = max(toe_strength, 0.01)
                diff = T - c_toe
                range_val = T - B
                c[mask_toe] = B + range_val * np.power(1.0 + k * diff / range_val, -1.0 / k)
            
            if result.ndim > 1:
                result[:, i] = c
            else:
                result[i] = c
        else:
            if c < low:
                T = low
                B = black_point
                k = max(toe_strength, 0.01)
                diff = T - c
                range_val = T - B
                if result.ndim > 1:
                    result[:, i] = B + range_val * np.power(1.0 + k * diff / range_val, -1.0 / k)
                else:
                    result[i] = B + range_val * np.power(1.0 + k * diff / range_val, -1.0 / k)
            elif c < black_point:
                if result.ndim > 1:
                    result[:, i] = black_point
                else:
                    result[i] = black_point
    
    max_c = np.max(np.abs(result)) if result.ndim > 1 else np.max(np.abs(result))
    
    if isinstance(max_c, np.ndarray):
        max_c = np.max(result, axis=-1) if result.ndim > 1 else max_c
    else:
        max_c = np.max(np.abs(result)) if result.ndim > 1 else np.max(np.abs(result))
    
    if max_c > high:
        mapped_c = max_c
        T = high
        W = white_point
        k = max(shoulder_strength, 0.01)
        diff = mapped_c - T
        range_val = W - T
        mapped_c = W - range_val * np.power(1.0 + k * diff / range_val, -1.0 / k)
        
        scale = mapped_c / max_c
        result *= scale
    
    return result


@pytest.mark.skipif(not HAS_EXPORTER, reason="Requires LUT exporter")
class TestHDRConsistency:
    """Golden checks for HDR-safe LUT exporter CPU math."""
    
    def test_sdr_ramp_grading(self):
        """Test grading on SDR ramp [0.0, 0.1, ..., 1.0]."""
        sdr_ramp = np.linspace(0.0, 1.0, 11).reshape(-1, 1)
        sdr_rgb = np.repeat(sdr_ramp, 3, axis=1)
        
        params = {
            'exposure': 50.0,
            'contrast': 20.0,
            'temp': 10.0,
            'tint': -5.0
        }
        
        worker = LutExportWorker()
        cpu_result = worker._apply_basic_grading(sdr_rgb, params)
        reference = _apply_grading_cpu_reference(sdr_rgb, params)
        
        assert cpu_result.shape == sdr_rgb.shape
        assert np.allclose(cpu_result, reference, atol=1e-6)
    
    def test_hdr_ramp_grading(self):
        """Test grading on HDR ramp [1.0, 1.5, 2.0, 3.0, 4.0]."""
        hdr_ramp = np.array([1.0, 1.5, 2.0, 3.0, 4.0]).reshape(-1, 1)
        hdr_rgb = np.repeat(hdr_ramp, 3, axis=1)
        
        params = {
            'exposure': 30.0,
            'contrast': -10.0
        }
        
        worker = LutExportWorker()
        cpu_result = worker._apply_basic_grading(hdr_rgb, params)
        reference = _apply_grading_cpu_reference(hdr_rgb, params)
        
        assert cpu_result.shape == hdr_rgb.shape
        assert np.allclose(cpu_result, reference, atol=1e-6)
        assert np.all(cpu_result > 0.0)
    
    def test_negative_ramp_grading(self):
        """Test grading on negative ramp [-0.3, -0.1, -0.05]."""
        neg_ramp = np.array([-0.3, -0.1, -0.05]).reshape(-1, 1)
        neg_rgb = np.repeat(neg_ramp, 3, axis=1)
        
        params = {
            'exposure': -10.0,
            'contrast': 5.0
        }
        
        worker = LutExportWorker()
        cpu_result = worker._apply_basic_grading(neg_rgb, params)
        reference = _apply_grading_cpu_reference(neg_rgb, params)
        
        assert cpu_result.shape == neg_rgb.shape
        assert np.allclose(cpu_result, reference, atol=1e-6)
        assert np.all(cpu_result >= -0.5)


@pytest.mark.skipif(not HAS_EXPORTER, reason="Requires LUT exporter")
class TestSoftClipConsistency:
    """Golden checks for Soft Clip exporter math."""
    
    def test_hdr_compression(self):
        """Test Soft Clip compressing HDR values (GPU-parity power shoulder)."""
        hdr_values = np.array([[2.0, 2.0, 2.0], [3.0, 3.0, 3.0], [4.0, 4.0, 4.0]])

        params = {
            'high': 0.9,
            'white_point': 1.0,
            'shoulder_strength': 0.5,
            'exposure': 0.0,
            'contrast': 0.0
        }

        worker = LutExportWorker()
        cpu_result = worker._apply_soft_clip_cpu(hdr_values, params)

        # 与 shader SOFT_CLIP_SNIPPET 相同的幂式 shoulder：mapped = W - rng*(1+k*d/rng)^(-1/k)
        T, W, k = 0.9, 1.0, 0.5
        rng = W - T
        expected = []
        for x in (2.0, 3.0, 4.0):
            mapped = W - rng * np.power(1.0 + k * (x - T) / rng, -1.0 / k)
            expected.append([mapped] * 3)
        expected = np.array(expected)

        assert np.all(cpu_result >= 0.0)
        assert np.allclose(cpu_result, expected, atol=1e-9)
        # 高光被压回白点以内（比例缩放，三通道同步 → 保饱和度）
        assert np.all(cpu_result <= W + 1e-9)
        assert np.all(cpu_result < hdr_values)
    
    def test_negative_recovery(self):
        """Test Soft Clip recovering negative values."""
        neg_values = np.array([[-0.2, -0.2, -0.2], [-0.1, -0.1, -0.1]])
        
        params = {
            'low': 0.1,
            'black_point': 0.0,
            'toe_strength': 0.5
        }
        
        worker = LutExportWorker()
        cpu_result = worker._apply_soft_clip_cpu(neg_values, params)
        
        assert np.all(cpu_result >= 0.0)


@pytest.mark.skipif(not HAS_EXPORTER, reason="Requires LUT exporter")
class TestExporterStructure:
    """Tests for LUT exporter structure and interface."""
    
    def test_exporter_initialization(self):
        """Test that LUT exporter can be initialized."""
        exporter = LUTExporter()
        assert exporter is not None
        exporter.cleanup()
    
    def test_apply_grading_cpu_exists(self):
        """Test that _apply_grading_cpu method exists."""
        worker = LutExportWorker()
        assert hasattr(worker, '_apply_grading_cpu')
    
    def test_apply_soft_clip_cpu_exists(self):
        """Test that _apply_soft_clip_cpu method exists."""
        worker = LutExportWorker()
        assert hasattr(worker, '_apply_soft_clip_cpu')
    
    def test_apply_mixer_cpu_exists(self):
        """Test that _apply_mixer_cpu method exists."""
        worker = LutExportWorker()
        assert hasattr(worker, '_apply_mixer_cpu')

    def test_extract_exportable_params_includes_hsl_curve_lut(self):
        """HSL Curves are pixel-independent and must be exported."""
        class FakeLayer:
            pass

        hsl_curve_lut = np.ones((6, 256), dtype=np.float32)
        layer = FakeLayer()
        layer.grading_params = {}
        layer.curves_lut = None
        layer.hsl_curve_lut = hsl_curve_lut
        layer.mixer_params = {}
        layer.soft_clip_params = {}
        layer.lut_intensity = 1.0
        layer.active_lut_path = None

        exporter = object.__new__(LUTExporter)
        params = exporter._extract_exportable_params(layer)

        assert 'hsl_curve_lut' in params
        assert np.array_equal(params['hsl_curve_lut'], hsl_curve_lut)


ROOT = Path(__file__).resolve().parents[2]


def test_hdr_math_contract_document_exists():
    """Test that HDR math contract document exists."""
    contract_path = ROOT / 'docs' / 'hdr_math_contract.md'
    assert contract_path.exists(), f"HDR math contract not found at {contract_path}"


def test_phase2_keyer_hsl_consistency():
    """Phase 2: Verify keyer.glsl uses HSL semantics for qualifier/keyer."""
    keyer_path = ROOT / 'src' / 'features' / 'color' / 'shaders' / 'keyer.glsl'

    with open(keyer_path, 'r') as f:
        keyer_code = f.read()

    assert 'rgb_to_hsl' in keyer_code, "keyer.glsl should use rgb_to_hsl"
    assert 'vec3 hsl = rgb_to_hsl' in keyer_code, "keyer.glsl should use HSL variable name"


def test_phase2_shader_nodes_hsv_comment():
    """Phase 2: Verify shader_nodes.py has HSV comment."""
    nodes_path = ROOT / 'src' / 'features' / 'color' / 'core' / 'shader_nodes.py'
    
    with open(nodes_path, 'r') as f:
        nodes_code = f.read()
    
    assert 'HSV (Hue, Saturation, Value)' in nodes_code, "shader_nodes.py should document HSV semantics"
    assert 'Phase 3' in nodes_code or 'Phase 2' in nodes_code, "shader_nodes.py should reference Phase"
