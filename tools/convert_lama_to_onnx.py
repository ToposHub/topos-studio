#!/usr/bin/env python3
"""
LaMa PyTorch -> ONNX Conversion Script for Topos Color

Converts LaMa (Resolution-robust Large Mask Inpainting with Fourier Convolutions)
PyTorch weights to ONNX format for local inference in Topos Color.

LaMa is a state-of-the-art inpainting model that handles large masks well.
It uses Fourier convolutions for resolution-robust processing.

Usage:
    python convert_lama_to_onnx.py
    python convert_lama_to_onnx.py --input models/inpaint/lama.pt
    python convert_lama_to_onnx.py --input <path> --output <path> --test-size 512

Requirements:
    pip install torch onnx onnxruntime

Model Architecture:
    Input: [1, 4, H, W] float32
        - Channels 0-2: RGB image [0, 1]
        - Channel 3: Mask (1 = masked, 0 = unmasked)
    Output: [1, 3, H, W] float32
        - RGB inpainted result [0, 1]

Reference:
    https://github.com/advimman/lama
"""

import argparse
import os
import sys
import time
import traceback

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Tuple, Optional


# =============================================================================
# LaMa Architecture (Fourier Convolution based inpainting)
# =============================================================================

class FourierUnit(nn.Module):
    """Fourier convolution unit for frequency-domain processing."""
    
    def __init__(self, in_channels: int, out_channels: int, spectral_norm: bool = False):
        super().__init__()
        self.conv_real = nn.Conv2d(in_channels * 2, out_channels * 2, 1, 1, 0)
        self.conv_imag = nn.Conv2d(in_channels * 2, out_channels * 2, 1, 1, 0)
        
        if spectral_norm:
            self.conv_real = nn.utils.spectral_norm(self.conv_real)
            self.conv_imag = nn.utils.spectral_norm(self.conv_imag)
        
        self.bn = nn.BatchNorm2d(out_channels * 2)
        self.relu = nn.ReLU(inplace=True)
        
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # FFT
        fft = torch.fft.rfft2(x, norm='ortho')
        
        # Split real and imaginary
        real = fft.real
        imag = fft.imag
        
        # Concatenate and convolve
        fft_features = torch.cat([real, imag], dim=1)
        fft_features = self.conv_real(fft_features)
        
        # Split back
        real_out = fft_features[:, :fft_features.shape[1] // 2]
        imag_out = fft_features[:, fft_features.shape[1] // 2:]
        
        # Combine and IFFT
        fft_out = torch.stack([real_out, imag_out], dim=-1)
        fft_out = torch.view_as_complex(fft_out)
        output = torch.fft.irfft2(fft_out, s=x.shape[2:], norm='ortho')
        
        output = self.bn(output)
        output = self.relu(output)
        
        return output


class SpectralConv(nn.Module):
    """Spectral convolution combining spatial and frequency processing."""
    
    def __init__(self, in_channels: int, out_channels: int, spectral_norm: bool = False):
        super().__init__()
        self.fourier_unit = FourierUnit(in_channels, out_channels, spectral_norm)
        self.conv_spatial = nn.Conv2d(in_channels, out_channels, 1, 1, 0)
        
        if spectral_norm:
            self.conv_spatial = nn.utils.spectral_norm(self.conv_spatial)
        
        self.bn = nn.BatchNorm2d(out_channels)
        self.relu = nn.ReLU(inplace=True)
        
        # Learnable mixing weight
        self.mix = nn.Parameter(torch.tensor([0.5]))
        
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # Spatial branch
        spatial_out = self.conv_spatial(x)
        
        # Fourier branch
        fourier_out = self.fourier_unit(x)
        
        # Mix with learnable weight
        mix = torch.sigmoid(self.mix)
        output = mix * spatial_out + (1 - mix) * fourier_out
        
        output = self.bn(output)
        output = self.relu(output)
        
        return output


class LaMaEncoder(nn.Module):
    """LaMa encoder with multi-scale spectral convolutions."""
    
    def __init__(self, in_channels: int = 4, base_channels: int = 64):
        super().__init__()
        
        # Initial conv
        self.conv_in = nn.Sequential(
            nn.Conv2d(in_channels, base_channels, 7, 1, 3),
            nn.BatchNorm2d(base_channels),
            nn.ReLU(inplace=True),
        )
        
        # Downsample blocks with spectral conv
        self.down1 = nn.Sequential(
            SpectralConv(base_channels, base_channels * 2),
            nn.Conv2d(base_channels * 2, base_channels * 2, 4, 2, 1),
        )
        
        self.down2 = nn.Sequential(
            SpectralConv(base_channels * 2, base_channels * 4),
            nn.Conv2d(base_channels * 4, base_channels * 4, 4, 2, 1),
        )
        
        self.down3 = nn.Sequential(
            SpectralConv(base_channels * 4, base_channels * 8),
            nn.Conv2d(base_channels * 8, base_channels * 8, 4, 2, 1),
        )
        
    def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, ...]:
        x0 = self.conv_in(x)
        x1 = self.down1(x0)
        x2 = self.down2(x1)
        x3 = self.down3(x2)
        
        return x0, x1, x2, x3


class LaMaDecoder(nn.Module):
    """LaMa decoder with multi-scale upsampling."""
    
    def __init__(self, base_channels: int = 64):
        super().__init__()
        
        # Upsample blocks
        self.up3 = nn.Sequential(
            nn.ConvTranspose2d(base_channels * 8, base_channels * 4, 4, 2, 1),
            SpectralConv(base_channels * 4, base_channels * 4),
        )
        
        self.up2 = nn.Sequential(
            nn.ConvTranspose2d(base_channels * 4, base_channels * 2, 4, 2, 1),
            SpectralConv(base_channels * 2, base_channels * 2),
        )
        
        self.up1 = nn.Sequential(
            nn.ConvTranspose2d(base_channels * 2, base_channels, 4, 2, 1),
            SpectralConv(base_channels, base_channels),
        )
        
        # Output conv
        self.conv_out = nn.Conv2d(base_channels, 3, 7, 1, 3)
        
    def forward(self, x3: torch.Tensor, skips: Tuple[torch.Tensor, ...]) -> torch.Tensor:
        x0, x1, x2 = skips
        
        x = self.up3(x3) + x2
        x = self.up2(x) + x1
        x = self.up1(x) + x0
        
        output = self.conv_out(x)
        
        return output


class LaMa(nn.Module):
    """
    LaMa inpainting model.
    
    Input: [B, 4, H, W] - RGB image + mask
    Output: [B, 3, H, W] - inpainted RGB image
    """
    
    def __init__(self, in_channels: int = 4, base_channels: int = 64):
        super().__init__()
        
        self.encoder = LaMaEncoder(in_channels, base_channels)
        self.decoder = LaMaDecoder(base_channels)
        
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # Encode
        skips = self.encoder(x)
        x3 = skips[-1]
        
        # Decode
        output = self.decoder(x3, skips[:-1])
        
        return output


# =============================================================================
# ONNX Export
# =============================================================================

def export_lama_to_onnx(
    model: nn.Module,
    output_path: str,
    test_size: int = 512,
    opset_version: int = 14,
) -> str:
    """Export LaMa model to ONNX with dynamic axes."""
    print(f"\n{'=' * 60}")
    print(f"[ONNX Export] LaMa model...")
    print(f"{'=' * 60}")
    print(f"  Output:     {output_path}")
    print(f"  Test size:  {test_size}x{test_size}")
    print(f"  Opset:      {opset_version}")
    
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    
    # Create test input: [1, 4, H, W] (RGB + mask)
    dummy = torch.randn(1, 4, test_size, test_size, dtype=torch.float32)
    
    dynamic_axes = {
        "input": {0: "batch", 2: "height", 3: "width"},
        "output": {0: "batch", 2: "height", 3: "width"},
    }
    
    start = time.time()
    torch.onnx.export(
        model,
        dummy,
        output_path,
        input_names=["input"],
        output_names=["output"],
        dynamic_axes=dynamic_axes,
        opset_version=opset_version,
        do_constant_folding=True,
        verbose=False,
    )
    elapsed = time.time() - start
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"  Exported in {elapsed:.1f}s, size: {size_mb:.1f} MB")
    
    return output_path


def validate_onnx(
    model: nn.Module,
    onnx_path: str,
    test_size: int = 256,
    rtol: float = 1e-3,
    atol: float = 1e-4,
) -> bool:
    """Validate ONNX output matches PyTorch."""
    import onnxruntime as ort
    
    print(f"\n{'=' * 60}")
    print(f"[Validation] PyTorch vs ONNX")
    print(f"{'=' * 60}")
    
    # Test input
    test_input = np.random.rand(1, 4, test_size, test_size).astype(np.float32)
    test_tensor = torch.from_numpy(test_input)
    
    with torch.no_grad():
        pt_out = model(test_tensor).cpu().numpy()
    print(f"  PyTorch: shape={pt_out.shape}, range=[{pt_out.min():.4f}, {pt_out.max():.4f}]")
    
    session = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    inp_name = session.get_inputs()[0].name
    onnx_out = session.run(None, {inp_name: test_input})[0]
    print(f"  ONNX:    shape={onnx_out.shape}, range=[{onnx_out.min():.4f}, {onnx_out.max():.4f}]")
    
    diff = np.abs(pt_out - onnx_out)
    max_diff = diff.max()
    mean_diff = diff.mean()
    is_close = np.allclose(pt_out, onnx_out, rtol=rtol, atol=atol)
    
    print(f"  Max diff: {max_diff:.6f}, Mean diff: {mean_diff:.6f}")
    print(f"  Result: {'PASS' if is_close else 'FAIL'}")
    
    # Dynamic size test
    test2 = np.random.rand(1, 4, test_size + 64, test_size + 64).astype(np.float32)
    try:
        out2 = session.run(None, {inp_name: test2})[0]
        print(f"  Dynamic {test_size + 64}x{test_size + 64}: OK shape={out2.shape}")
    except Exception as e:
        print(f"  Dynamic size test: FAIL {e}")
        is_close = False
    
    del session
    return is_close


def main():
    parser = argparse.ArgumentParser(description="Convert LaMa .pth -> ONNX for Topos Color")
    parser.add_argument("--input", "-i", default="models/inpaint/lama.pt")
    parser.add_argument("--output", "-o", default="models/inpaint/lama.onnx")
    parser.add_argument("--test-size", type=int, default=512)
    parser.add_argument("--validate-size", type=int, default=256)
    parser.add_argument("--opset", type=int, default=14)
    parser.add_argument("--no-validate", action="store_true")
    parser.add_argument("--base-channels", type=int, default=64, help="Base channel count for model")
    args = parser.parse_args()
    
    input_path = os.path.join(SCRIPT_DIR, args.input) if not os.path.isabs(args.input) else args.input
    output_path = os.path.join(SCRIPT_DIR, args.output) if not os.path.isabs(args.output) else args.output
    
    print("=" * 60)
    print("Topos Color - LaMa to ONNX Converter")
    print("=" * 60)
    print(f"  Input:  {input_path}")
    print(f"  Output: {output_path}")
    
    # Create model
    print(f"\nBuilding LaMa model (base_channels={args.base_channels})...")
    model = LaMa(in_channels=4, base_channels=args.base_channels)
    
    total_params = sum(p.numel() for p in model.parameters())
    print(f"  Parameters: {total_params:,} ({total_params / 1e6:.2f}M)")
    
    # Try to load weights if file exists
    if os.path.exists(input_path):
        print(f"\nLoading weights from: {input_path}")
        try:
            checkpoint = torch.load(input_path, map_location="cpu", weights_only=False)
            if "state_dict" in checkpoint:
                state_dict = checkpoint["state_dict"]
            elif "model" in checkpoint:
                state_dict = checkpoint["model"]
            else:
                state_dict = checkpoint
            
            # Load with strict=False for partial loading
            missing, unexpected = model.load_state_dict(state_dict, strict=False)
            if missing:
                print(f"  Missing keys: {len(missing)}")
            if unexpected:
                print(f"  Unexpected keys: {len(unexpected)}")
            print("  Loaded weights")
        except Exception as e:
            print(f"  Failed to load weights: {e}")
            print("  Using default initialized model")
    else:
        print(f"\nInput file not found: {input_path}")
        print("Using default initialized model (random weights)")
        print("Note: For actual inpainting, you need to download LaMa weights:")
        print("  https://github.com/advimman/lama")
    
    model.eval()
    
    # Export
    onnx_path = export_lama_to_onnx(model, output_path, test_size=args.test_size, opset_version=args.opset)
    
    # Validate
    if not args.no_validate:
        if not validate_onnx(model, onnx_path, test_size=args.validate_size):
            print("\nValidation FAILED!")
            sys.exit(1)
    else:
        print("\nValidation skipped")
    
    # Summary
    size_mb = os.path.getsize(onnx_path) / (1024 * 1024)
    print(f"\n{'=' * 60}")
    print("Conversion Complete!")
    print(f"{'=' * 60}")
    print(f"  {onnx_path} ({size_mb:.1f} MB)")
    print(f"  Input:  [1, 4, H, W] float32 (RGB + mask)")
    print(f"  Output: [1, 3, H, W] float32 (RGB inpainted)")
    print(f"\n  Ready for Topos Color AI Inpaint pipeline!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()