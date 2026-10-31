#!/usr/bin/env python3
"""
NAFNet PyTorch -> ONNX Conversion Script for Topos Color

Converts NAFNet .pth weights to ONNX format,
compatible with the project's ONNX Runtime inference pipeline.

Usage:
    python convert_nafnet_to_onnx.py
    python convert_nafnet_to_onnx.py --input models/denoise/NAFNet-SIDD-width32.pth
    python convert_nafnet_to_onnx.py --input <path> --output <path> --test-size 256

Requirements:
    pip install torch onnx onnxruntime
"""

import argparse
import os
import sys
import time

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import torch
import torch.nn as nn
import torch.nn.functional as F

from src.core.ai_models.nafnet import NAFBlock, SimpleGate


class LayerNorm2dONNX(nn.Module):
    """LayerNorm2d variant that is ONNX-export friendly."""
    def __init__(self, channels, eps=1e-6):
        super().__init__()
        self.channels = channels
        self.eps = eps
        self.weight = nn.Parameter(torch.ones(channels))
        self.bias = nn.Parameter(torch.zeros(channels))

    def forward(self, x):
        # Use mean/var computation directly to avoid dynamic shape in F.layer_norm
        mean = x.mean(dim=1, keepdim=True)
        var = x.var(dim=1, keepdim=True, unbiased=False)
        x = (x - mean) / torch.sqrt(var + self.eps)
        w = self.weight.view(1, -1, 1, 1)
        b = self.bias.view(1, -1, 1, 1)
        return x * w + b


class NAFBlockONNX(NAFBlock):
    """NAFBlock with ONNX-friendly LayerNorm."""
    def __init__(self, c, DW_Expand=2, FFN_Expand=2, drop_out_rate=0.):
        super().__init__(c, DW_Expand, FFN_Expand, drop_out_rate)
        # Replace norm layers with ONNX-friendly versions
        self.norm1 = LayerNorm2dONNX(c)
        self.norm2 = LayerNorm2dONNX(c)


class NAFNetONNX(nn.Module):
    """NAFNet variant for ONNX export (ONNX-friendly LayerNorm)."""
    def __init__(self, img_channel=3, width=32, middle_blk_num=1, enc_blk_nums=None, dec_blk_nums=None):
        super().__init__()
        if enc_blk_nums is None:
            enc_blk_nums = [1, 1, 1, 1]
        if dec_blk_nums is None:
            dec_blk_nums = [1, 1, 1, 1]

        self.intro = nn.Conv2d(in_channels=img_channel, out_channels=width, kernel_size=3, padding=1, stride=1, groups=1, bias=True)
        self.ending = nn.Conv2d(in_channels=width, out_channels=img_channel, kernel_size=3, padding=1, stride=1, groups=1, bias=True)

        self.encoders = nn.ModuleList()
        self.decoders = nn.ModuleList()
        self.middle_blks = nn.ModuleList()
        self.ups = nn.ModuleList()
        self.downs = nn.ModuleList()

        chan = width
        for num in enc_blk_nums:
            self.encoders.append(
                nn.Sequential(*[NAFBlockONNX(chan) for _ in range(num)])
            )
            self.downs.append(
                nn.Conv2d(chan, 2 * chan, 2, 2)
            )
            chan = chan * 2

        self.middle_blks = nn.Sequential(
            *[NAFBlockONNX(chan) for _ in range(middle_blk_num)]
        )

        for num in dec_blk_nums:
            self.ups.append(
                nn.Sequential(
                    nn.Conv2d(chan, chan * 2, 1, bias=False),
                    nn.PixelShuffle(2)
                )
            )
            chan = chan // 2
            self.decoders.append(
                nn.Sequential(*[NAFBlockONNX(chan) for _ in range(num)])
            )

        self.padder_size = 2 ** len(self.encoders)

    def forward(self, inp):
        B, C, H, W = inp.shape
        inp = self.check_image_size(inp)

        x = self.intro(inp)

        encs = []
        for encoder, down in zip(self.encoders, self.downs):
            x = encoder(x)
            encs.append(x)
            x = down(x)

        x = self.middle_blks(x)

        for decoder, up, enc_skip in zip(self.decoders, self.ups, encs[::-1]):
            x = up(x)
            x = x + enc_skip
            x = decoder(x)

        x = self.ending(x)
        x = x + inp

        return x[:, :, :H, :W]

    def check_image_size(self, x):
        _, _, h, w = x.size()
        mod_pad_h = (self.padder_size - h % self.padder_size) % self.padder_size
        mod_pad_w = (self.padder_size - w % self.padder_size) % self.padder_size
        x = F.pad(x, (0, mod_pad_w, 0, mod_pad_h))
        return x


def detect_nafnet_arch(state_dict):
    """Auto-detect NAFNet architecture parameters from a state_dict."""
    print("\n" + "=" * 60)
    print("[Auto-Detect] Analyzing state_dict architecture...")
    print("=" * 60)

    # width from intro.weight shape
    width = state_dict["intro.weight"].shape[0]
    img_channel = state_dict["intro.weight"].shape[1]
    print(f"  width       = {width}")
    print(f"  img_channel = {img_channel}")

    # Count encoder/decoder/middle blocks
    from collections import defaultdict
    enc_blocks = defaultdict(set)
    dec_blocks = defaultdict(set)
    mid_blks = set()

    for k in state_dict.keys():
        parts = k.split(".")
        if k.startswith("encoders."):
            enc_blocks[int(parts[1])].add(int(parts[2]))
        elif k.startswith("decoders."):
            dec_blocks[int(parts[1])].add(int(parts[2]))
        elif k.startswith("middle_blks."):
            mid_blks.add(int(parts[1]))

    enc_blk_nums = [len(enc_blocks[i]) for i in sorted(enc_blocks)]
    dec_blk_nums = [len(dec_blocks[i]) for i in sorted(dec_blocks)]
    middle_blk_num = len(mid_blks)

    print(f"  enc_blk_nums  = {enc_blk_nums}")
    print(f"  dec_blk_nums  = {dec_blk_nums}")
    print(f"  middle_blk_num = {middle_blk_num}")

    return {
        "img_channel": img_channel,
        "width": width,
        "middle_blk_num": middle_blk_num,
        "enc_blk_nums": enc_blk_nums,
        "dec_blk_nums": dec_blk_nums,
    }


def export_to_onnx(model, output_path, test_size=256, opset_version=14):
    """Export model to ONNX with dynamic axes."""
    print(f"\n{'=' * 60}")
    print(f"[ONNX Export] Starting...")
    print(f"{'=' * 60}")
    print(f"  Output:     {output_path}")
    print(f"  Test size:  {test_size}x{test_size}")
    print(f"  Opset:      {opset_version}")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    # NAFNet needs input size divisible by 2^len(encoders) = 16
    padder = 2 ** len(model.encoders)
    padded_size = ((test_size + padder - 1) // padder) * padder
    img_channel = model.intro.weight.shape[1]
    dummy = torch.randn(1, img_channel, padded_size, padded_size, dtype=torch.float32)

    dynamic_axes = {
        "input": {0: "batch", 2: "height", 3: "width"},
        "output": {0: "batch", 2: "height", 3: "width"},
    }

    start = time.time()
    torch.onnx.export(
        model, dummy, output_path,
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


def validate_onnx(model, onnx_path, test_size=128, rtol=1e-3, atol=1e-4):
    """Validate ONNX output matches PyTorch."""
    import onnxruntime as ort

    print(f"\n{'=' * 60}")
    print(f"[Validation] PyTorch vs ONNX")
    print(f"{'=' * 60}")

    # Pad to divisible size
    padder = 2 ** len(model.encoders)
    padded = ((test_size + padder - 1) // padder) * padder
    img_channel = model.intro.weight.shape[1]

    test_input = np.random.rand(1, img_channel, padded, padded).astype(np.float32)
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

    # Dynamic resolution test
    sz2 = 192
    padded2 = ((sz2 + padder - 1) // padder) * padder
    test2 = np.random.rand(1, img_channel, padded2, padded2).astype(np.float32)
    try:
        out2 = session.run(None, {inp_name: test2})[0]
        print(f"  Dynamic {padded2}x{padded2}: OK shape={out2.shape}")
    except Exception as e:
        print(f"  Dynamic {padded2}x{padded2}: FAIL {e}")
        is_close = False

    del session
    return is_close


def main():
    parser = argparse.ArgumentParser(description="Convert NAFNet .pth -> ONNX for Topos Color")
    parser.add_argument("--input", "-i", default="models/denoise/NAFNet-SIDD-width32.pth")
    parser.add_argument("--output", "-o", default=None,
                        help="Output path (default: same as input with .onnx extension)")
    parser.add_argument("--test-size", type=int, default=256)
    parser.add_argument("--validate-size", type=int, default=128)
    parser.add_argument("--opset", type=int, default=14)
    parser.add_argument("--no-validate", action="store_true")
    args = parser.parse_args()

    input_path = os.path.join(SCRIPT_DIR, args.input) if not os.path.isabs(args.input) else args.input

    # Default output: same filename with .onnx extension
    if args.output is None:
        output_path = os.path.splitext(input_path)[0] + ".onnx"
    else:
        output_path = os.path.join(SCRIPT_DIR, args.output) if not os.path.isabs(args.output) else args.output

    print("=" * 60)
    print("Topos Color - NAFNet to ONNX Converter")
    print("=" * 60)
    print(f"  Input:  {input_path}")
    print(f"  Output: {output_path}")

    if not os.path.exists(input_path):
        print(f"\nFile not found: {input_path}")
        sys.exit(1)

    # Load checkpoint
    print(f"\nLoading: {input_path}")
    checkpoint = torch.load(input_path, map_location="cpu", weights_only=False)

    if "params" in checkpoint:
        state_dict = checkpoint["params"]
        print(f"  Format: 'params' key")
    elif "state_dict" in checkpoint:
        state_dict = checkpoint["state_dict"]
        print(f"  Format: 'state_dict' key")
    else:
        state_dict = checkpoint
        print(f"  Format: raw state_dict")
    print(f"  Keys: {len(state_dict)}")

    # Detect architecture
    arch = detect_nafnet_arch(state_dict)

    # Build model
    print(f"\nBuilding NAFNet model (ONNX-friendly)...")
    model = NAFNetONNX(**arch)
    total_params = sum(p.numel() for p in model.parameters())
    print(f"  Parameters: {total_params:,} ({total_params / 1e6:.2f}M)")

    # Load weights
    print(f"\nLoading weights...")
    result = model.load_state_dict(state_dict, strict=True)
    print(f"  Loaded successfully")

    model.eval()
    print(f"Model ready")

    # Export
    onnx_path = export_to_onnx(model, output_path, test_size=args.test_size, opset_version=args.opset)

    # Validate
    if not args.no_validate:
        if not validate_onnx(model, onnx_path, test_size=args.validate_size):
            print(f"\nValidation FAILED!")
            sys.exit(1)
    else:
        print(f"\nValidation skipped")

    # Summary
    size_mb = os.path.getsize(onnx_path) / (1024 * 1024)
    print(f"\n{'=' * 60}")
    print(f"Conversion Complete!")
    print(f"{'=' * 60}")
    print(f"  {onnx_path} ({size_mb:.1f} MB)")
    print(f"  Input:  [1, 3, H, W] float32 [0, 1]")
    print(f"  Output: [1, 3, H, W] float32 [0, 1]")
    print(f"\n  Ready for Topos Color AI Denoise pipeline!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
