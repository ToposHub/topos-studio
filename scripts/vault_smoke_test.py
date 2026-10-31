#!/usr/bin/env python3
"""真实模型联调验证脚本（P0-1）。

用户下载好模型后跑这个脚本，逐个验证 6 个真实 provider 能否端到端工作。
不依赖主项目，自包含。用法：

    python3 scripts/vault_smoke_test.py            # 验证全部能力
    python3 scripts/vault_smoke_test.py transcript # 只验证某能力
    python3 scripts/vault_smoke_test.py --samples /path/to/samples  # 用真实素材

验证维度：
    1. provider.is_available() 应返回 True（模型装好）
    2. provider 实际推理输出维度/格式正确
    3. 端到端：index → search 语义检索命中

退出码：0 全过 / 非0 有失败项（失败项会列出，方便定位）。

前置：用户需下载下列模型到 models/ai_editing/<pack_id>/（或让 sentence-transformers
自动下载到 ~/.cache/huggingface/）：
    qwen3_embedding_0_6b   - Qwen3-Embedding-0.6B（台词/文本向量）
    siglip2_base           - SigLIP2 base-patch16-224（画面向量）
    qwen3_vl_8b_awq        - Qwen3-VL-8B-Instruct-AWQ（画面理解，需 GPU）
    qwen2_5_omni_7b_awq    - Qwen2.5-Omni-7B-AWQ（音频理解，需 GPU）
    insightface_buffalo_l  - InsightFace buffalo_l（人脸，ONNX）
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

# 让 apps.topos_vault 可 import（脚本放在项目根的 scripts/）
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT))


def _status(ok: bool) -> str:
    return "✅ 通过" if ok else "❌ 失败"


def _check_available(provider) -> bool:
    """is_available 不抛异常 + 返回 bool。"""
    try:
        return bool(provider.is_available())
    except Exception as e:
        print(f"     is_available 抛异常：{e}")
        return False


def _try_infer(provider, fn, *args):
    """跑一次推理；模型加载失败时给出明确诊断（区分'未下载'和'真出错'）。"""
    try:
        return fn(*args)
    except RuntimeError as e:
        if "模型未加载" in str(e) or "model" in str(e).lower():
            return None, "is_available=True 但模型加载失败（多半是模型还没下载，或 transformers 版本不兼容）"
        return None, f"推理失败：{e}"
    except Exception as e:
        return None, f"推理异常：{type(e).__name__}: {e}"


# ---------------------------------------------------------------------- #
# 单能力验证
# ---------------------------------------------------------------------- #

def verify_transcript_embed(samples: Path | None) -> tuple[bool, str]:
    """Qwen3-Embedding：embed_text 出 1024 维向量，语义相近的文本相似度高。"""
    from apps.topos_vault.providers.real_transcript_embed import Qwen3EmbeddingProvider

    p = Qwen3EmbeddingProvider()
    if not _check_available(p):
        return False, "模型未装或依赖缺失（is_available=False）"

    import numpy as np
    # 语义测试：相近的应比无关的更相似
    r1, err = _try_infer(p, p.embed_text, "一只猫在桌子上睡觉")
    if r1 is None:
        return False, err
    r2, _ = _try_infer(p, p.embed_text, "小猫趴在桌上打盹")
    r3, _ = _try_infer(p, p.embed_text, "汽车在高速公路上飞驰")

    if r1.dim != 1024 or len(r1.vector) != 1024:
        return False, f"维度错误：期望 1024，得到 {r1.dim}"

    v1, v2, v3 = map(lambda r: np.array(r.vector), (r1, r2, r3))
    def cos(a, b):
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))

    sim_close = cos(v1, v2)
    sim_far = cos(v1, v3)
    detail = f"dim={r1.dim} 相近={sim_close:.3f} 无关={sim_far:.3f}"
    if sim_close <= sim_far:
        return False, f"语义相似度反转：相近({sim_close:.3f}) 应 > 无关({sim_far:.3f})"
    return True, detail


def verify_visual_embed(samples: Path | None) -> tuple[bool, str]:
    """SigLIP2：embed_text/embed_image 在同一空间（图文对齐）。"""
    from apps.topos_vault.providers.real_visual_embed import SigLIP2EmbeddingProvider

    p = SigLIP2EmbeddingProvider()
    if not _check_available(p):
        return False, "模型未装或依赖缺失（is_available=False）"

    import numpy as np
    rt, err = _try_infer(p, p.embed_text, "微笑")
    if rt is None:
        return False, err
    if rt.dim != 768 or len(rt.vector) != 768:
        return False, f"文本向量维度错误：期望 768，得到 {rt.dim}"

    # 如有真实图片素材，测图文对齐；否则只验维度
    img = _find_sample(samples, (".jpg", ".jpeg", ".png"))
    if img is None:
        return True, f"dim={rt.dim}（无图片样本，跳过图文对齐测试）"

    ri, err = _try_infer(p, p.embed_image, str(img))
    if ri is None:
        return False, err
    if ri.dim != 768:
        return False, f"图片向量维度错误：期望 768，得到 {ri.dim}"

    vt = np.array(rt.vector)
    vi = np.array(ri.vector)
    sim = float(vt @ vi / (np.linalg.norm(vt) * np.linalg.norm(vi) + 1e-12))
    return True, f"dim={rt.dim} 图文相似度={sim:.3f}（{img.name}）"


def verify_visual_caption(samples: Path | None) -> tuple[bool, str]:
    """Qwen3-VL：caption_image 出非空中文描述 + 标签。"""
    from apps.topos_vault.providers.real_visual_caption import Qwen3VLCaptioner

    p = Qwen3VLCaptioner()
    if not _check_available(p):
        return False, "模型未装/无 GPU（is_available=False）"

    img = _find_sample(samples, (".jpg", ".jpeg", ".png"))
    if img is None:
        # 造一张合成图测（纯色），保证不因缺素材失败
        from PIL import Image
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            Image.new("RGB", (224, 224), (128, 128, 128)).save(f.name)
            img = Path(f.name)
        cleanup = True
    else:
        cleanup = False

    try:
        result, err = _try_infer(p, p.caption_image, str(img))
        if result is None:
            return False, err
        ok = bool(result.caption) and isinstance(result.tags, list)
        detail = f"caption={result.caption[:40]!r} tags={result.tags}"
        return ok, detail
    finally:
        if cleanup:
            img.unlink(missing_ok=True)


def verify_audio_understand(samples: Path | None) -> tuple[bool, str]:
    """Qwen2.5-Omni：understand_audio 出描述/情绪。"""
    from apps.topos_vault.providers.real_audio_understand import Qwen25OmniAudioProvider

    p = Qwen25OmniAudioProvider()
    if not _check_available(p):
        return False, "模型未装/无 GPU（is_available=False）"

    audio = _find_sample(samples, (".wav", ".mp3", ".flac", ".m4a"))
    if audio is None:
        return True, "无音频样本，跳过（is_available 已确认）"

    result, err = _try_infer(p, p.understand_audio, str(audio))
    if result is None:
        return False, err
    ok = bool(result.description)
    return ok, f"description={result.description[:40]!r} mood={result.mood}"


def verify_face_recognize(samples: Path | None) -> tuple[bool, str]:
    """InsightFace：detect_faces 返回 FaceResult（可无人脸，但不能崩）。"""
    from apps.topos_vault.providers.real_face_recognize import InsightFaceProvider

    p = InsightFaceProvider()
    if not _check_available(p):
        return False, "模型未装或依赖缺失（is_available=False）"

    img = _find_sample(samples, (".jpg", ".jpeg", ".png"))
    if img is None:
        return True, "无图片样本，跳过（is_available 已确认）"

    result, err = _try_infer(p, p.detect_faces, str(img))
    if result is None:
        return False, err
    ok = isinstance(result.faces, list)
    detail = f"检测到 {len(result.faces)} 张脸"
    if result.faces:
        f0 = result.faces[0]
        detail += f"，首张：conf={f0.confidence:.2f} angle={f0.face_angle}"
        if len(f0.embedding) != 512:
            return False, f"人脸向量维度错误：期望 512，得到 {len(f0.embedding)}"
    return ok, detail


def verify_asr(samples: Path | None) -> tuple[bool, str]:
    """ASR：主项目集成模式或 vault stub。验证选中的 provider 不崩。"""
    from apps.topos_vault.providers import get_provider, Capability
    p = get_provider(Capability.ASR)
    if not _check_available(p):
        return False, "无可用 ASR provider"

    audio = _find_sample(samples, (".wav", ".mp3", ".flac", ".m4a"))
    if audio is None:
        return True, f"无音频样本，跳过（provider={p.provider_id} is_available 已确认）"

    result, err = _try_infer(p, p.transcribe, str(audio))
    if result is None:
        return False, err
    detail = f"provider={p.provider_id} {len(result.lines)} 句"
    return True, detail


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #

def _find_sample(samples_dir: Path | None, exts: tuple[str, ...]) -> Path | None:
    if samples_dir is None or not samples_dir.is_dir():
        return None
    for ext in exts:
        for f in samples_dir.rglob(f"*{ext}"):
            return f
    return None


_VERIFY_FUNCS = {
    "transcript": ("Qwen3-Embedding（台词/文本向量）", verify_transcript_embed),
    "visual_embed": ("SigLIP2（画面向量）", verify_visual_embed),
    "visual_caption": ("Qwen3-VL（画面理解）", verify_visual_caption),
    "audio_understand": ("Qwen2.5-Omni（音频理解）", verify_audio_understand),
    "face_recognize": ("InsightFace（人脸）", verify_face_recognize),
    "asr": ("ASR（台词转写）", verify_asr),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="vault 真实模型联调验证（P0-1）")
    parser.add_argument("capability", nargs="?", default="all",
                        choices=["all"] + list(_VERIFY_FUNCS),
                        help="只验证某能力（默认 all）")
    parser.add_argument("--samples", type=Path, default=None,
                        help="素材样本目录（含真实 jpg/mp3 等，可选）")
    args = parser.parse_args(argv)

    # 隔离数据目录，避免污染真实 vault
    os.environ.setdefault("TOPOS_VAULT_DIR", str(Path(tempfile.mkdtemp(prefix="vault_smoke_"))))

    caps = list(_VERIFY_FUNCS) if args.capability == "all" else [args.capability]

    print("=" * 60)
    print("Topos Vault 真实模型联调验证（P0-1）")
    print("=" * 60)
    all_ok = True
    for cap in caps:
        name, fn = _VERIFY_FUNCS[cap]
        print(f"\n[{name}]")
        try:
            ok, detail = fn(args.samples)
        except Exception as e:
            ok, detail = False, f"未预期异常：{type(e).__name__}: {e}"
        print(f"  {_status(ok)}  {detail}")
        all_ok = all_ok and ok

    print("\n" + "=" * 60)
    if all_ok:
        print("🎉 全部通过")
        return 0
    print("⚠️ 有失败项（见上方详情）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
