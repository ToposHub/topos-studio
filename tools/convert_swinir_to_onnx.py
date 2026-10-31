#!/usr/bin/env python3
"""
SwinIR PyTorch -> ONNX Conversion Script for Topos Color

Converts official SwinIR denoising .pth weights to ONNX format,
fully compatible with the project's ONNX Runtime inference pipeline.

This script includes the OFFICIAL SwinIR architecture from the authors' repo,
because the project's simplified swinir.py has a different structure.

Usage:
    python convert_swinir_to_onnx.py
    python convert_swinir_to_onnx.py --input models/denoise/005_colorDN_DFWB_s128w8_SwinIR-M_noise50.pth
    python convert_swinir_to_onnx.py --input <path> --output <path> --test-size 256

Requirements:
    pip install torch einops onnx onnxruntime
"""

import argparse
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import numpy as np

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


# =============================================================================
# Official SwinIR Architecture (from JingyunLiang/SwinIR)
# Included here because the project's swinir.py uses a different structure.
# =============================================================================

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange


def window_partition(x: torch.Tensor, window_size: int) -> torch.Tensor:
    """Partition into non-overlapping windows with padding if needed."""
    B, H, W, C = x.shape
    pad_h = (window_size - H % window_size) % window_size
    pad_w = (window_size - W % window_size) % window_size
    if pad_h > 0 or pad_w > 0:
        x = F.pad(x, (0, 0, 0, pad_w, 0, pad_h))
    Hp, Wp = H + pad_h, W + pad_w
    x = x.view(B, Hp // window_size, window_size, Wp // window_size, window_size, C)
    windows = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size, window_size, C)
    return windows, Hp, Wp


def window_reverse(windows: torch.Tensor, window_size: int, H: int, W: int, Hp: int, Wp: int) -> torch.Tensor:
    """Merge windows back to feature map."""
    B = int(windows.shape[0] / (Hp * Wp / window_size / window_size))
    x = windows.view(B, Hp // window_size, Wp // window_size, window_size, window_size, -1)
    x = x.permute(0, 1, 3, 2, 4, 5).contiguous().view(B, Hp, Wp, -1)
    if Hp > H or Wp > W:
        x = x[:, :H, :W, :].contiguous()
    return x


class Mlp(nn.Module):
    def __init__(self, in_features: int, hidden_features: Optional[int] = None,
                 out_features: Optional[int] = None, act_layer=nn.GELU, drop: float = 0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        return x


def window_mask(H: int, W: int, window_size: int, shift_size: int) -> Optional[torch.Tensor]:
    """Calculate attention mask for SW-MSA."""
    img_mask = torch.zeros((1, H, W, 1))
    h_slices = (slice(0, -window_size),
                slice(-window_size, -shift_size),
                slice(-shift_size, None))
    w_slices = (slice(0, -window_size),
                slice(-window_size, -shift_size),
                slice(-shift_size, None))
    cnt = 0
    for h in h_slices:
        for w in w_slices:
            img_mask[:, h, w, :] = cnt
            cnt += 1
    mask_windows = img_mask.view(1, H // window_size, window_size, W // window_size, window_size, 1)
    mask_windows = mask_windows.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, window_size, window_size, 1)
    mask_windows = mask_windows.view(-1, window_size * window_size)
    attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)
    attn_mask = attn_mask.masked_fill(attn_mask != 0, float(-100.0)).masked_fill(attn_mask == 0, float(0.0))
    return attn_mask


class WindowAttention(nn.Module):
    def __init__(self, dim: int, window_size: Tuple[int, int], num_heads: int,
                 qkv_bias: bool = True, qk_scale: Optional[float] = None,
                 attn_drop: float = 0., proj_drop: float = 0.):
        super().__init__()
        self.dim = dim
        self.window_size = window_size
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        # Relative position bias
        self.relative_position_bias_table = nn.Parameter(
            torch.zeros((2 * window_size[0] - 1) * (2 * window_size[1] - 1), num_heads))

        coords_h = torch.arange(self.window_size[0])
        coords_w = torch.arange(self.window_size[1])
        coords = torch.stack(torch.meshgrid([coords_h, coords_w], indexing='ij'))
        coords_flatten = torch.flatten(coords, 1)
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()
        relative_coords[:, :, 0] += self.window_size[0] - 1
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1
        relative_position_index = relative_coords.sum(-1)
        self.register_buffer("relative_position_index", relative_position_index)

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        nn.init.trunc_normal_(self.relative_position_bias_table, std=.02)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x: torch.Tensor, mask: Optional[torch.Tensor] = None) -> torch.Tensor:
        B_, N, C = x.shape
        qkv = self.qkv(x).reshape(B_, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]

        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))

        relative_position_bias = self.relative_position_bias_table[
            self.relative_position_index.view(-1)].view(
            self.window_size[0] * self.window_size[1],
            self.window_size[0] * self.window_size[1], -1)
        relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()
        attn = attn + relative_position_bias.unsqueeze(0)

        if mask is not None:
            nW = mask.shape[0]
            attn = attn.view(B_ // nW, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)
            attn = self.softmax(attn)
        else:
            attn = self.softmax(attn)

        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(1, 2).reshape(B_, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class SwinTransformerBlock(nn.Module):
    def __init__(self, dim: int, num_heads: int, window_size: int = 7,
                 shift_size: int = 0, mlp_ratio: float = 4.,
                 qkv_bias: bool = True, qk_scale: Optional[float] = None,
                 drop: float = 0., attn_drop: float = 0., drop_path: float = 0.,
                 act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.window_size = window_size
        self.shift_size = shift_size
        self.mlp_ratio = mlp_ratio

        self.norm1 = norm_layer(dim)
        self.attn = WindowAttention(
            dim, window_size=(self.window_size, self.window_size), num_heads=num_heads,
            qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop)

        self.drop_path = nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)

    def forward(self, x: torch.Tensor, x_size: Tuple[int, int]) -> torch.Tensor:
        H, W = x_size
        B, L, C = x.shape

        shortcut = x
        x = self.norm1(x)
        x = x.view(B, H, W, C)

        # Pad to window size
        pad_r = (self.window_size - W % self.window_size) % self.window_size
        pad_b = (self.window_size - H % self.window_size) % self.window_size
        Hp, Wp = H + pad_b, W + pad_r
        if pad_r > 0 or pad_b > 0:
            x = F.pad(x, (0, 0, 0, pad_r, 0, pad_b))


        if self.shift_size > 0:
            shifted_x = torch.roll(x, shifts=(-self.shift_size, -self.shift_size), dims=(1, 2))
            attn_mask = window_mask(Hp, Wp, self.window_size, self.shift_size).to(x.device)
        else:
            shifted_x = x
            attn_mask = None

        x_windows = window_partition(shifted_x, self.window_size)[0]
        x_windows = x_windows.view(-1, self.window_size * self.window_size, C)

        attn_windows = self.attn(x_windows, mask=attn_mask)

        attn_windows = attn_windows.view(-1, self.window_size, self.window_size, C)
        shifted_x = window_reverse(attn_windows, self.window_size, Hp, Wp, Hp, Wp)

        if self.shift_size > 0:
            x = torch.roll(shifted_x, shifts=(self.shift_size, self.shift_size), dims=(1, 2))
        else:
            x = shifted_x

        # Remove padding
        if pad_r > 0 or pad_b > 0:
            x = x[:, :H, :W, :].contiguous()

        x = x.view(B, H * W, C)
        x = shortcut + self.drop_path(x)
        x = x + self.drop_path(self.mlp(self.norm2(x)))

        return x


class _ResidualGroup(nn.Module):
    """Wrapper that holds the blocks - matches official 'residual_group' naming."""

    def __init__(self, blocks: nn.ModuleList):
        super().__init__()
        self.blocks = blocks

    def forward(self, x: torch.Tensor, x_size: Tuple[int, int]) -> torch.Tensor:
        for blk in self.blocks:
            x = blk(x, x_size)
        return x


class RSTB(nn.Module):
    """Residual Swin Transformer Block (RSTB) - matches official naming."""

    def __init__(self, dim: int, depth: int, num_heads: int, window_size: int,
                 mlp_ratio: float = 4., qkv_bias: bool = True, qk_scale: Optional[float] = None,
                 drop: float = 0., attn_drop: float = 0., drop_path: float = 0.,
                 act_layer=nn.GELU, norm_layer=nn.LayerNorm):
        super().__init__()
        self.dim = dim
        self.depth = depth

        # Use residual_group wrapper to match official key naming:
        # layers.{i}.residual_group.blocks.{j}.*.*
        blocks = nn.ModuleList([
            SwinTransformerBlock(dim=dim,
                                 num_heads=num_heads,
                                 window_size=window_size,
                                 shift_size=0 if (i % 2 == 0) else window_size // 2,
                                 mlp_ratio=mlp_ratio,
                                 qkv_bias=qkv_bias, qk_scale=qk_scale,
                                 drop=drop, attn_drop=attn_drop,
                                 drop_path=drop_path,
                                 act_layer=act_layer,
                                 norm_layer=norm_layer)
            for i in range(depth)])
        self.residual_group = _ResidualGroup(blocks)

        self.conv = nn.Conv2d(dim, dim, 3, 1, 1)

    def forward(self, x: torch.Tensor, x_size: Tuple[int, int]) -> torch.Tensor:
        x = self.residual_group(x, x_size)
        x = x.transpose(1, 2).view(x.shape[0], -1, x_size[0], x_size[1])
        x = self.conv(x)
        x = x.flatten(2).transpose(1, 2)
        return x


class SwinIROfficial(nn.Module):
    """Official SwinIR model for denoising / super-resolution."""

    def __init__(self, img_size: int = 64, patch_size: int = 1, in_chans: int = 3,
                 embed_dim: int = 180, depths: List[int] = [6, 6, 6, 6, 6, 6],
                 num_heads: List[int] = [6, 6, 6, 6, 6, 6],
                 window_size: int = 8, mlp_ratio: float = 4.,
                 qkv_bias: bool = True, qk_scale: Optional[float] = None,
                 drop_rate: float = 0., attn_drop_rate: float = 0., drop_path_rate: float = 0.1,
                 norm_layer=nn.LayerNorm, ape: bool = False, patch_norm: bool = True,
                 use_checkpoint: bool = False, upscale: int = 1,
                 img_range: float = 1., resi_connection: str = '1conv',
                 upsampler: str = '', **kwargs):
        super().__init__()
        self.window_size = window_size
        self.embed_dim = embed_dim
        self.upscale = upscale
        self.img_range = img_range

        # Mean subtraction
        if in_chans == 3:
            rgb_mean = (0.4488, 0.4371, 0.4040)
            self.mean = torch.Tensor(rgb_mean).view(1, 3, 1, 1)
        else:
            self.mean = torch.zeros(1, 1, 1, 1)

        # Head
        self.conv_first = nn.Conv2d(in_chans, embed_dim, 3, 1, 1)

        # Patch embedding (for official format compatibility)
        self.patch_embed = nn.Module()
        self.patch_embed.norm = norm_layer(embed_dim) if patch_norm else nn.Identity()

        self.pos_drop = nn.Dropout(p=drop_rate)

        # Stochastic depth
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, sum(depths))]

        # Build RSTB layers
        self.layers = nn.ModuleList()
        for i_layer in range(len(depths)):
            layer = RSTB(
                dim=embed_dim,
                depth=depths[i_layer],
                num_heads=num_heads[i_layer],
                window_size=window_size,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate,
                drop_path=dpr[sum(depths[:i_layer]):sum(depths[:i_layer + 1])],
                norm_layer=norm_layer,
            )
            self.layers.append(layer)

        self.norm = norm_layer(embed_dim)

        # Residual connection body
        if resi_connection == '1conv':
            self.conv_after_body = nn.Conv2d(embed_dim, embed_dim, 3, 1, 1)
        elif resi_connection == '3conv':
            self.conv_after_body = nn.Sequential(
                nn.Conv2d(embed_dim, embed_dim // 4, 3, 1, 1),
                nn.LeakyReLU(negative_slope=0.2, inplace=True),
                nn.Conv2d(embed_dim // 4, embed_dim // 4, 1, 1, 0),
                nn.LeakyReLU(negative_slope=0.2, inplace=True),
                nn.Conv2d(embed_dim // 4, embed_dim, 3, 1, 1)
            )

        # Tail (no upsampling for denoising)
        if upscale > 1:
            self.conv_before_upsample = nn.Sequential(
                nn.Conv2d(embed_dim, 64, 3, 1, 1),
                nn.LeakyReLU(inplace=True))
            if upsampler == 'pixelshuffle':
                self.upsample = nn.Sequential(
                    nn.Conv2d(64, in_chans * upscale * upscale, 3, 1, 1),
                    nn.PixelShuffle(upscale))
            else:
                raise NotImplementedError(f"Upsampler {upsampler} not supported")
        else:
            self.conv_last = nn.Conv2d(embed_dim, in_chans, 3, 1, 1)

        self.apply(self._init_weights)

    def _init_weights(self, m: nn.Module) -> None:
        if isinstance(m, nn.Linear):
            nn.init.trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward_features(self, x: torch.Tensor) -> torch.Tensor:
        x_size = (x.shape[2], x.shape[3])
        x = x.flatten(2).transpose(1, 2)
        x = self.pos_drop(x)

        for layer in self.layers:
            x = layer(x, x_size)

        x = self.norm(x)
        x = x.transpose(1, 2).view(x.shape[0], -1, x_size[0], x_size[1])
        return x

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        self.mean = self.mean.type_as(x)
        x = (x - self.mean) * self.img_range

        x = self.conv_first(x)
        x = self.conv_after_body(self.forward_features(x)) + x

        if self.upscale > 1:
            x = self.conv_before_upsample(x)
            x = self.upsample(x)
        else:
            x = self.conv_last(x)

        x = x / self.img_range + self.mean
        return x


# =============================================================================
# Architecture Detection
# =============================================================================

def detect_swinir_arch(state_dict: Dict) -> Dict:
    """
    Auto-detect SwinIR architecture parameters from a state_dict.

    Args:
        state_dict: PyTorch model state dict

    Returns:
        Dict of architecture parameters for SwinIROfficial constructor
    """
    print("\n" + "=" * 60)
    print("🔍 [Auto-Detect] Analyzing state_dict architecture...")
    print("=" * 60)

    # 1. embed_dim and in_chans
    if "conv_first.weight" not in state_dict:
        raise ValueError("Cannot find 'conv_first.weight'. Is this a valid SwinIR model?")
    conv_shape = state_dict["conv_first.weight"].shape
    embed_dim = conv_shape[0]
    in_chans = conv_shape[1]
    print(f"  embed_dim  = {embed_dim}")
    print(f"  in_chans   = {in_chans}")

    # 2. Detect layers and depths from residual_group.blocks pattern
    layer_indices = set()
    block_counts: Dict[int, int] = {}
    for key in state_dict.keys():
        # Match: layers.{i}.residual_group.blocks.{j}.* (official format)
        parts = key.split(".")
        if len(parts) >= 5 and parts[0] == "layers":
            if parts[1].isdigit():
                layer_idx = int(parts[1])
                layer_indices.add(layer_idx)
                # Check for residual_group.blocks.{j} pattern
                if "blocks" in parts:
                    try:
                        blk_idx = parts.index("blocks")
                        block_idx = int(parts[blk_idx + 1])
                        if layer_idx not in block_counts:
                            block_counts[layer_idx] = 0
                        block_counts[layer_idx] = max(block_counts[layer_idx], block_idx + 1)
                    except (ValueError, IndexError):
                        pass

    num_layers = len(layer_indices)
    depths = [block_counts.get(i, 0) for i in sorted(layer_indices)]
    print(f"  num_layers = {num_layers}")
    print(f"  depths     = {depths}")

    # 3. window_size and num_heads
    window_size = None
    num_heads = None
    for key in state_dict.keys():
        if "relative_position_bias_table" in key:
            table_shape = state_dict[key].shape
            table_len = table_shape[0]
            num_heads = table_shape[1]
            side = int(np.sqrt(table_len))
            if side * side == table_len:
                window_size = (side + 1) // 2
            print(f"  bias_table shape: {table_shape} -> window_size={window_size}, num_heads={num_heads}")
            break

    if window_size is None:
        window_size = 8
    if num_heads is None:
        num_heads = 6

    num_heads_list = [num_heads] * num_layers
    print(f"  window_size = {window_size}")
    print(f"  num_heads   = {num_heads_list}")

    # 4. upscale
    upscale = 1
    if "upsample.0.weight" in state_dict:
        upscale = int(np.sqrt(state_dict["upsample.0.weight"].shape[0] // in_chans))
    print(f"  upscale    = {upscale}")

    # 5. resi_connection
    resi_connection = "1conv"
    if "conv_after_body.0.weight" in state_dict:
        resi_connection = "3conv"
    print(f"  resi_connection = {resi_connection}")

    # 6. Auto-detect mlp_ratio from mlp.fc1.weight
    mlp_ratio = 4.0  # default
    for key in state_dict.keys():
        if "mlp.fc1.weight" in key:
            fc1_shape = state_dict[key].shape
            hidden_dim = fc1_shape[0]
            mlp_ratio = hidden_dim / embed_dim
            print(f"  mlp_ratio  = {mlp_ratio} (from {key}: {fc1_shape})")
            break

    arch_params = {
        "img_size": 64,
        "patch_size": 1,
        "in_chans": in_chans,
        "embed_dim": embed_dim,
        "depths": depths,
        "num_heads": num_heads_list,
        "window_size": window_size,
        "mlp_ratio": mlp_ratio,
        "upscale": upscale,
        "img_range": 1.0,
        "resi_connection": resi_connection,
        "ape": False,
        "patch_norm": True,
        "use_checkpoint": False,
    }

    print("\n✅ [Auto-Detect] Complete:")
    for k, v in arch_params.items():
        print(f"    {k:20s} = {v}")

    return arch_params


def export_to_onnx(
    model: nn.Module,
    output_path: str,
    test_size: int = 256,
    opset_version: int = 14,
) -> str:
    """Export model to ONNX with dynamic axes."""
    print(f"\n{'=' * 60}")
    print(f"[ONNX Export] Starting...")
    print(f"{'=' * 60}")
    print(f"  Output:     {output_path}")
    print(f"  Test size:  {test_size}x{test_size}")
    print(f"  Opset:      {opset_version}")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    in_chans = model.conv_first.weight.shape[1]
    dummy = torch.randn(1, in_chans, test_size, test_size, dtype=torch.float32)

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
    print(f"  ✅ Exported in {elapsed:.1f}s, size: {size_mb:.1f} MB")

    return output_path


def validate_onnx(
    model: nn.Module,
    onnx_path: str,
    test_size: int = 128,
    rtol: float = 1e-3,
    atol: float = 1e-4,
) -> bool:
    """Validate ONNX output matches PyTorch."""
    import onnxruntime as ort

    print(f"\n{'=' * 60}")
    print(f"[Validation] PyTorch vs ONNX")
    print(f"{'=' * 60}")

    in_chans = model.conv_first.weight.shape[1]
    test_input = np.random.rand(1, in_chans, test_size, test_size).astype(np.float32)
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
    print(f"  Result: {'✅ PASS' if is_close else '❌ FAIL'}")

    # Dynamic resolution test
    sz2 = test_size + 64
    test2 = np.random.rand(1, in_chans, sz2, sz2).astype(np.float32)
    try:
        out2 = session.run(None, {inp_name: test2})[0]
        print(f"  Dynamic {sz2}x{sz2}: ✅ shape={out2.shape}")
    except Exception as e:
        print(f"  Dynamic {sz2}x{sz2}: ❌ {e}")
        is_close = False

    del session
    return is_close


def main():
    parser = argparse.ArgumentParser(description="Convert SwinIR .pth -> ONNX for Topos Color")
    parser.add_argument("--input", "-i", default="models/denoise/005_colorDN_DFWB_s128w8_SwinIR-M_noise50.pth")
    parser.add_argument("--output", "-o", default="models/denoise/swinir_denoise.onnx")
    parser.add_argument("--test-size", type=int, default=256)
    parser.add_argument("--validate-size", type=int, default=128)
    parser.add_argument("--opset", type=int, default=14)
    parser.add_argument("--no-validate", action="store_true")
    args = parser.parse_args()

    input_path = os.path.join(SCRIPT_DIR, args.input) if not os.path.isabs(args.input) else args.input
    output_path = os.path.join(SCRIPT_DIR, args.output) if not os.path.isabs(args.output) else args.output

    print("=" * 60)
    print("🎨 Topos Color - SwinIR to ONNX Converter")
    print("=" * 60)
    print(f"  Input:  {input_path}")
    print(f"  Output: {output_path}")

    if not os.path.exists(input_path):
        print(f"\n❌ File not found: {input_path}")
        sys.exit(1)

    # Load checkpoint
    print(f"\n📂 Loading: {input_path}")
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
    arch = detect_swinir_arch(state_dict)

    # Build model
    print(f"\n🏗️ Building official SwinIR model...")
    model = SwinIROfficial(**arch)
    total_params = sum(p.numel() for p in model.parameters())
    print(f"  Parameters: {total_params:,} ({total_params / 1e6:.2f}M)")

    # Load weights
    print(f"\n📦 Loading weights...")
    result = model.load_state_dict(state_dict, strict=False)
    if result.unexpected_keys:
        print(f"  ⚠️ {len(result.unexpected_keys)} unexpected keys (ok if just attn_mask buffers)")
    if result.missing_keys:
        print(f"  ⚠️ {len(result.missing_keys)} missing keys:")
        for k in result.missing_keys:
            print(f"    - {k}")

    model.eval()
    print(f"✅ Model ready")

    # Export
    onnx_path = export_to_onnx(model, output_path, test_size=args.test_size, opset_version=args.opset)

    # Validate
    if not args.no_validate:
        if not validate_onnx(model, onnx_path, test_size=args.validate_size):
            print(f"\n⚠️ Validation FAILED!")
            sys.exit(1)
    else:
        print(f"\n⏭️ Validation skipped")

    # Summary
    size_mb = os.path.getsize(onnx_path) / (1024 * 1024)
    print(f"\n{'=' * 60}")
    print(f"🎉 Conversion Complete!")
    print(f"{'=' * 60}")
    print(f"  📄 {onnx_path} ({size_mb:.1f} MB)")
    print(f"  🧩 Input:  [1, 3, H, W] float32 [0, 1]")
    print(f"  🧩 Output: [1, 3, H, W] float32 [0, 1]")
    print(f"\n  💡 Ready for Topos Color AI Denoise pipeline!")
    print(f"     Select from: Denoise Panel → AI Mode → Model dropdown")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()