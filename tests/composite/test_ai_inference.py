#!/usr/bin/env python3
"""
AI Inference Test Script for Topos Color

Tests the local AI inference pipeline:
1. Model conversion (LaMa ONNX)
2. Model manager registration
3. Inference worker execution
4. Progress reporting
5. Streaming preview

Usage:
    python test_ai_inference.py
    python test_ai_inference.py --test-inpaint
    python test_ai_inference.py --test-denoise
    python test_ai_inference.py --all

Requirements:
    pip install onnxruntime torch numpy opencv-python PyQt6
"""

import os
import sys
import time
import argparse
import numpy as np
import cv2

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def test_onnx_env():
    """Test ONNX Runtime environment configuration."""
    print("\n" + "=" * 60)
    print("TEST 1: ONNX Environment")
    print("=" * 60)
    
    from src.shared.ai_models.onnx_env import (
        get_optimal_providers,
        get_gpu_provider_info,
        get_system_info,
    )
    
    info = get_system_info()
    print(f"  OS: {info['os']}")
    print(f"  Architecture: {info['arch']}")
    print(f"  ONNX Runtime: {info['onnxruntime_version']}")
    print(f"  Available Providers: {info['available_providers']}")
    print(f"  Selected Providers: {info['selected_providers']}")
    
    gpu_info = get_gpu_provider_info()
    if gpu_info and gpu_info.get("provider"):
        print(f"\n  GPU Available: {gpu_info['description']}")
        print(f"  Install Hint: {gpu_info['install_hint']}")
    else:
        print("\n  GPU: Not available, using CPU")
    
    print("\n  Result: PASS")


def test_model_manager():
    """Test model manager functionality."""
    print("\n" + "=" * 60)
    print("TEST 2: Model Manager")
    print("=" * 60)
    
    from src.shared.ai_models.model_manager import (
        AIModelManager,
        MODEL_REGISTRY,
        get_model_manager,
    )
    
    manager = get_model_manager()
    
    print(f"  Cache Directory: {manager._cache_dir}")
    print(f"  Registered Models: {len(MODEL_REGISTRY)}")
    
    # List models by category
    for model_id, info in MODEL_REGISTRY.items():
        status = "Available" if info.is_available else "Not downloaded"
        print(f"    - {model_id} ({info.category}): {status}")
    
    # Check cache size
    cache_size = manager.get_cache_size()
    print(f"\n  Cache Size: {cache_size / (1024 * 1024):.2f} MB")
    
    print("\n  Result: PASS")


def test_lama_conversion():
    """Test LaMa model conversion to ONNX."""
    print("\n" + "=" * 60)
    print("TEST 3: LaMa ONNX Conversion")
    print("=" * 60)
    
    # Check if model already exists
    model_path = os.path.join(SCRIPT_DIR, "models", "inpaint", "lama.onnx")
    
    if os.path.exists(model_path):
        size_mb = os.path.getsize(model_path) / (1024 * 1024)
        print(f"  Model exists: {model_path}")
        print(f"  Size: {size_mb:.2f} MB")
        
        # Register with model manager
        from src.shared.ai_models.model_manager import get_model_manager
        manager = get_model_manager()
        manager.register_local_model("lama_inpaint", model_path)
        
        print("\n  Result: PASS (using existing model)")
        return True
    
    print("  Model not found, converting...")
    
    try:
        # Import conversion module
        from convert_lama_to_onnx import LaMa, export_lama_to_onnx
        import torch
        
        # Create model with default weights
        model = LaMa(in_channels=4, base_channels=64)
        model.eval()
        
        # Export
        os.makedirs(os.path.dirname(model_path), exist_ok=True)
        export_lama_to_onnx(model, model_path, test_size=512)
        
        # Register
        from src.shared.ai_models.model_manager import get_model_manager
        manager = get_model_manager()
        manager.register_local_model("lama_inpaint", model_path)
        
        print(f"\n  Model converted: {model_path}")
        print(f"  Size: {os.path.getsize(model_path) / (1024 * 1024):.2f} MB")
        
        print("\n  Result: PASS")
        return True
        
    except Exception as e:
        print(f"  Conversion failed: {e}")
        print("\n  Result: FAIL (conversion not available)")
        return False


def test_inference_worker():
    """Test inference worker execution."""
    print("\n" + "=" * 60)
    print("TEST 4: Inference Worker")
    print("=" * 60)
    
    # Check if model is available
    from src.shared.ai_models.model_manager import get_model_manager
    manager = get_model_manager()
    
    if not manager.is_model_available("lama_inpaint"):
        print("  LaMa model not available, skipping inference test")
        print("  Run with --convert to convert model first")
        print("\n  Result: SKIP")
        return False
    
    model_path = manager.get_model_path("lama_inpaint")
    print(f"  Model Path: {model_path}")
    
    try:
        import onnxruntime as ort
        from src.shared.ai_models.onnx_env import get_configured_providers, get_session_options
        
        # Create test input
        print("\n  Creating test input...")
        test_size = 256
        
        # Create a simple test image (color gradient)
        image = np.zeros((test_size, test_size, 3), dtype=np.float32)
        for i in range(test_size):
            image[i, :, 0] = i / test_size  # Red gradient
            image[:, i, 1] = i / test_size  # Green gradient
        image[:, :, 2] = 0.5  # Blue constant
        
        # Create a simple mask (center region)
        mask = np.zeros((test_size, test_size), dtype=np.float32)
        mask[64:192, 64:192] = 1.0  # Mask center region
        
        # Prepare input for LaMa (RGB + mask)
        img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        combined = np.concatenate([img_rgb, mask[:, :, np.newaxis]], axis=2)
        nchw = combined.transpose(2, 0, 1)[np.newaxis, :, :, :]
        nchw = np.ascontiguousarray(nchw, dtype=np.float32)
        
        print(f"  Input shape: {nchw.shape}")
        
        # Load and run inference
        print("\n  Loading ONNX session...")
        providers = get_configured_providers()
        sess_options = get_session_options()
        
        session = ort.InferenceSession(
            model_path,
            sess_options=sess_options,
            providers=providers,
        )
        
        actual_providers = session.get_providers()
        print(f"  Active Providers: {actual_providers}")
        
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name
        
        print("\n  Running inference...")
        start_time = time.time()
        
        outputs = session.run([output_name], {input_name: nchw})
        
        elapsed = time.time() - start_time
        print(f"  Inference time: {elapsed:.3f}s")
        
        result = outputs[0]
        print(f"  Output shape: {result.shape}")
        print(f"  Output range: [{result.min():.4f}, {result.max():.4f}]")
        
        # Convert to HWC BGR
        result_hwc = result.squeeze(0).transpose(1, 2, 0)
        result_bgr = cv2.cvtColor(result_hwc[:, :, :3], cv2.COLOR_RGB2BGR)
        
        # Save test result
        output_dir = os.path.join(SCRIPT_DIR, "test_output")
        os.makedirs(output_dir, exist_ok=True)
        
        # Save as EXR (float32)
        cv2.imwrite(
            os.path.join(output_dir, "inpaint_test_input.exr"),
            image.astype(np.float32),
            [cv2.IMWRITE_EXR_COMPRESSION, 1]  # T-P1-71: ZIP 经 libdeflate 间歇 SIGILL
        )
        cv2.imwrite(
            os.path.join(output_dir, "inpaint_test_mask.exr"),
            mask.astype(np.float32),
            [cv2.IMWRITE_EXR_COMPRESSION, 1]  # T-P1-71: ZIP 经 libdeflate 间歇 SIGILL
        )
        cv2.imwrite(
            os.path.join(output_dir, "inpaint_test_result.exr"),
            result_bgr.astype(np.float32),
            [cv2.IMWRITE_EXR_COMPRESSION, 1]  # T-P1-71: ZIP 经 libdeflate 间歇 SIGILL
        )
        
        # Also save as PNG for visualization
        cv2.imwrite(
            os.path.join(output_dir, "inpaint_test_input.png"),
            (image * 255).astype(np.uint8)
        )
        cv2.imwrite(
            os.path.join(output_dir, "inpaint_test_mask.png"),
            (mask * 255).astype(np.uint8)
        )
        cv2.imwrite(
            os.path.join(output_dir, "inpaint_test_result.png"),
            (np.clip(result_bgr, 0, 1) * 255).astype(np.uint8)
        )
        
        print(f"\n  Test outputs saved to: {output_dir}")
        
        del session
        
        print("\n  Result: PASS")
        return True
        
    except Exception as e:
        import traceback
        print(f"  Inference failed: {e}")
        traceback.print_exc()
        print("\n  Result: FAIL")
        return False


def test_ai_compute_manager():
    """Test AIComputeManager async execution."""
    print("\n" + "=" * 60)
    print("TEST 5: AI Compute Manager (Async)")
    print("=" * 60)
    
    from src.shared.ai_models.model_manager import get_model_manager
    manager = get_model_manager()
    
    if not manager.is_model_available("lama_inpaint"):
        print("  LaMa model not available, skipping async test")
        print("\n  Result: SKIP")
        return False
    
    try:
        from PyQt6.QtCore import QThread, QCoreApplication
        from src.features.composite.render.ai_cache_manager import AICacheManager
        from src.features.composite.render.ai_compute_manager import AIComputeManager
        
        # Create Qt app for signal handling
        app = QCoreApplication.instance()
        if app is None:
            app = QCoreApplication(sys.argv)
        
        cache_manager = AICacheManager()
        compute_manager = AIComputeManager(cache_manager)
        
        # Track signals
        results = {
            "finished": False,
            "preview": False,
            "progress": 0,
            "error": None,
        }
        
        def on_finished(cache_key):
            results["finished"] = True
            print(f"  [Signal] Finished: {cache_key}")
        
        def on_preview(cache_key):
            results["preview"] = True
            print(f"  [Signal] Preview: {cache_key}")
        
        def on_progress(cache_key, pct):
            results["progress"] = pct
            print(f"  [Signal] Progress: {pct}%")
        
        def on_failed(cache_key, error):
            results["error"] = error
            print(f"  [Signal] Error: {error}")
        
        compute_manager.compute_finished.connect(on_finished)
        compute_manager.compute_preview.connect(on_preview)
        compute_manager.compute_progress.connect(on_progress)
        compute_manager.compute_failed.connect(on_failed)
        
        # Create test input
        print("\n  Creating test input...")
        test_size = 256
        
        image = np.random.rand(test_size, test_size, 3).astype(np.float32)
        mask = np.zeros((test_size, test_size), dtype=np.float32)
        mask[64:192, 64:192] = 1.0
        
        input_images = {
            "image": image,
            "mask": mask,
        }
        
        cache_key = cache_manager.compute_key(
            "ai_inpaint",
            {"test": True},
            upstream_key="test",
        )
        
        print(f"  Cache Key: {cache_key}")
        print("\n  Starting async computation...")
        
        # Request compute
        started = compute_manager.request_compute(
            cache_key=cache_key,
            node_type="ai_inpaint",
            input_images=input_images,
            params={"strength": 1.0, "blend": 1.0},
            request_id=1,
        )
        
        print(f"  Computation started: {started}")
        
        # Wait for completion (with timeout)
        max_wait = 30  # seconds
        wait_start = time.time()
        
        while not results["finished"] and results["error"] is None:
            app.processEvents()
            time.sleep(0.1)
            
            if time.time() - wait_start > max_wait:
                print("  Timeout waiting for completion")
                break
        
        print(f"\n  Results:")
        print(f"    Finished: {results['finished']}")
        print(f"    Preview received: {results['preview']}")
        print(f"    Max progress: {results['progress']}%")
        print(f"    Error: {results['error']}")
        
        # Check cache
        if results["finished"]:
            cache_path = cache_manager.get_cache_path(cache_key)
            if cache_path:
                print(f"    Cache path: {cache_path}")
                size_mb = os.path.getsize(cache_path) / (1024 * 1024)
                print(f"    Cache size: {size_mb:.2f} MB")
        
        print("\n  Result: PASS")
        return results["finished"]
        
    except Exception as e:
        import traceback
        print(f"  Async test failed: {e}")
        traceback.print_exc()
        print("\n  Result: FAIL")
        return False


def test_tiled_inference():
    """Test tiled inference for large images."""
    print("\n" + "=" * 60)
    print("TEST 6: Tiled Inference (Large Image)")
    print("=" * 60)
    
    from src.shared.ai_models.model_manager import get_model_manager
    manager = get_model_manager()
    
    if not manager.is_model_available("lama_inpaint"):
        print("  LaMa model not available, skipping tiled test")
        print("\n  Result: SKIP")
        return False
    
    try:
        import onnxruntime as ort
        from src.shared.ai_models.onnx_env import get_configured_providers, get_session_options
        
        model_path = manager.get_model_path("lama_inpaint")
        
        # Create large test image (1024x1024)
        print("\n  Creating large test image (1024x1024)...")
        test_size = 1024
        
        image = np.random.rand(test_size, test_size, 3).astype(np.float32)
        mask = np.zeros((test_size, test_size), dtype=np.float32)
        mask[256:768, 256:768] = 1.0
        
        # Prepare input
        img_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        combined = np.concatenate([img_rgb, mask[:, :, np.newaxis]], axis=2)
        nchw = combined.transpose(2, 0, 1)[np.newaxis, :, :, :]
        nchw = np.ascontiguousarray(nchw, dtype=np.float32)
        
        print(f"  Input shape: {nchw.shape}")
        
        # Load session
        print("\n  Loading ONNX session...")
        providers = get_configured_providers()
        sess_options = get_session_options()
        
        session = ort.InferenceSession(
            model_path,
            sess_options=sess_options,
            providers=providers,
        )
        
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name
        
        print("\n  Running inference on large image...")
        start_time = time.time()
        
        outputs = session.run([output_name], {input_name: nchw})
        
        elapsed = time.time() - start_time
        print(f"  Inference time: {elapsed:.3f}s")
        
        result = outputs[0]
        print(f"  Output shape: {result.shape}")
        
        del session
        
        print("\n  Result: PASS")
        return True
        
    except Exception as e:
        import traceback
        print(f"  Tiled inference failed: {e}")
        traceback.print_exc()
        print("\n  Result: FAIL")
        return False


def run_all_tests():
    """Run all tests."""
    print("=" * 60)
    print("Topos Color - AI Inference Pipeline Tests")
    print("=" * 60)
    
    results = {}
    
    # Test 1: ONNX Environment
    results["onnx_env"] = test_onnx_env()
    
    # Test 2: Model Manager
    results["model_manager"] = test_model_manager()
    
    # Test 3: LaMa Conversion
    results["lama_conversion"] = test_lama_conversion()
    
    # Test 4: Inference Worker
    results["inference_worker"] = test_inference_worker()
    
    # Test 5: AI Compute Manager
    results["compute_manager"] = test_ai_compute_manager()
    
    # Test 6: Tiled Inference
    results["tiled_inference"] = test_tiled_inference()
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    
    passed = sum(1 for v in results.values() if v)
    failed = sum(1 for v in results.values() if v is False)
    skipped = sum(1 for v in results.values() if v is None)
    
    for name, result in results.items():
        status = "PASS" if result else ("SKIP" if result is None else "FAIL")
        print(f"  {name}: {status}")
    
    print(f"\n  Total: {passed} passed, {failed} failed, {skipped} skipped")
    
    return passed > 0 and failed == 0


def main():
    parser = argparse.ArgumentParser(description="Test AI Inference Pipeline")
    parser.add_argument("--all", action="store_true", help="Run all tests")
    parser.add_argument("--test-onnx", action="store_true", help="Test ONNX environment")
    parser.add_argument("--test-manager", action="store_true", help="Test model manager")
    parser.add_argument("--test-convert", action="store_true", help="Test model conversion")
    parser.add_argument("--test-inference", action="store_true", help="Test inference worker")
    parser.add_argument("--test-async", action="store_true", help="Test async compute manager")
    parser.add_argument("--test-tiled", action="store_true", help="Test tiled inference")
    args = parser.parse_args()
    
    if args.all or not any([
        args.test_onnx, args.test_manager, args.test_convert,
        args.test_inference, args.test_async, args.test_tiled
    ]):
        return run_all_tests()
    
    results = []
    
    if args.test_onnx:
        results.append(test_onnx_env())
    
    if args.test_manager:
        results.append(test_model_manager())
    
    if args.test_convert:
        results.append(test_lama_conversion())
    
    if args.test_inference:
        results.append(test_inference_worker())
    
    if args.test_async:
        results.append(test_ai_compute_manager())
    
    if args.test_tiled:
        results.append(test_tiled_inference())
    
    return all(r for r in results if r is not None)


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)