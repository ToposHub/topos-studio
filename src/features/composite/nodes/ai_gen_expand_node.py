"""AI Gen Expand node (C1) — generative outpainting / masked fill.

Wraps the shared :mod:`CloudImageGenJob` so the composite graph can send a
generation request (Volcengine Ark or any transport with the same
contract) without importing edit.  The transport + key provider are
injected process-wide via :func:`set_generation_transport`; without them
the node sits in a visible NEEDS_CONFIG state and passes its input
through — never a silent failure, never a crash.

Execution model: jobs run through the injected transport on a short-lived
worker thread with a hard timeout, so a network round trip (or a hung
connection) never blocks the render queue indefinitely; results are
cached content-addressed (prompt+seed+model+input identity) in the node
disk cache, so repeat frames/exports hit the cache with no network and
no full-frame readback.  The request carries only the mask — providers
never see the input frame — so this node enforces the outside-mask
guarantee itself (input*(1-mask) + generated*mask) before uploading.

时域一致性（plan C1）：静帧/逐帧独立生成——视频逐帧独立生成会闪烁，
适用场景是延展画幅、生成空镜、静帧修复。
"""

import logging
import threading
from typing import Any, Callable, Dict, Optional, Tuple

import numpy as np

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

from ..render.disk_cache_store import get_disk_cache_store

logger = logging.getLogger(__name__)

# Process-wide injection (wired by the page from preferences/env; tests
# inject stubs).  Signature: (request: dict, api_key: str,
# output_size: (w, h)) -> RGBA float32 array.
GenerationTransport = Callable[[Dict[str, Any], str, Any], "np.ndarray"]
_TRANSPORT: Optional[GenerationTransport] = None
_KEY_PROVIDER: Optional[Callable[[], str]] = None
_COST_CONFIRM: Optional[Callable[[int, str], bool]] = None

# AUD-6312: the three injections form ONE logical group.  Replacement used
# to be three bare assignments, so a render-thread reader could observe a
# mixed snapshot ("new transport + old key provider") while the UI thread
# re-injected.  All writers hold this lock; readers take the group snapshot
# through :func:`_generation_stack` under the same lock.
_TRANSPORT_LOCK = threading.Lock()

# AUD-6307: bound on how long one generation may stall the render queue.
# The transport call is a cloud round trip (seconds to tens of seconds);
# without a bound a hung connection blocked preview/export/playback
# forever.  On expiry the node degrades visibly (FAILED + passthrough).
_GENERATION_TIMEOUT_S = 120.0

# Cache-entry prefix for the node's own composited results (the raw
# provider output lives under the job's "gen{key}" entry).
_COMPOSITED_ENTRY_PREFIX = "gencomp"


def set_generation_transport(transport: Optional[GenerationTransport],
                             key_provider: Optional[Callable[[], str]] = None,
                             cost_confirm: Optional[Callable[[int, str],
                                                             bool]] = None
                             ) -> None:
    """Install the process transport (production: Volcengine adapter).

    Threading contract (AUD-6307/6308): the transport and
    ``cost_confirm`` are invoked on a short-lived worker thread — never
    on the render thread.  An interactive cost confirmer must marshal to
    the UI thread itself (e.g. via a Qt signal) before touching widgets.

    AUD-6312: the replacement of the (transport, key_provider,
    cost_confirm) group is atomic under :data:`_TRANSPORT_LOCK`, so a
    render-thread reader via :func:`_generation_stack` never observes a
    mixed old/new combination mid-replacement.
    """
    global _TRANSPORT, _KEY_PROVIDER, _COST_CONFIRM
    with _TRANSPORT_LOCK:
        _TRANSPORT = transport
        _KEY_PROVIDER = key_provider
        _COST_CONFIRM = cost_confirm


def _generation_stack(
) -> Tuple[Optional[GenerationTransport],
           Optional[Callable[[], str]],
           Optional[Callable[[int, str], bool]]]:
    """Consistent snapshot of the injected transport stack (AUD-6312).

    Render-thread readers must read the group through this helper instead
    of touching the module globals directly: it holds
    :data:`_TRANSPORT_LOCK` while copying, so the three values always
    belong to the same :func:`set_generation_transport` call.
    """
    with _TRANSPORT_LOCK:
        return _TRANSPORT, _KEY_PROVIDER, _COST_CONFIRM


def reset_generation_transport() -> None:
    set_generation_transport(None, None, None)


# ── 生产接线（AUD-6311）─────────────────────────────────────────────

def _decode_image_to_rgba(handle: str, output_size: Tuple[int, int],
                          fetch_timeout_s: float = 60.0) -> "np.ndarray":
    """Decode a provider candidate (file path 或 URL) into RGBA float32.

    返回值约定与节点 transport 契约一致：H×W×4、float32、值域 [0,1]。
    尺寸与请求的 output_size 不一致时（provider 有最小像素数下限，会把
    小图上采样）用 PIL 重采样回请求尺寸——遮罩合成路径要求生成帧与输入
    帧同形，否则只能可见降级。
    """
    from io import BytesIO

    from PIL import Image

    data: bytes
    if handle.startswith(("http://", "https://")):
        import urllib.request

        with urllib.request.urlopen(handle, timeout=fetch_timeout_s) as resp:
            data = resp.read()
    else:
        with open(handle, "rb") as fh:
            data = fh.read()

    with Image.open(BytesIO(data)) as img:
        rgba = img.convert("RGBA")
        width, height = int(output_size[0]), int(output_size[1])
        if width > 0 and height > 0 and rgba.size != (width, height):
            rgba = rgba.resize((width, height))
        return np.asarray(rgba, dtype=np.float32) / 255.0


def install_default_generation_transport(
    *,
    config: Optional[Dict[str, Any]] = None,
    credential_store: Any = None,
    provider_factory: Any = None,
) -> bool:
    """安装生产 transport + key provider（AUD-6311）。

    之前 ``set_generation_transport`` 在生产代码中零调用：节点注册进
    NODE_REGISTRY 可被用户创建，但没有 page/适配器注入 transport，生产
    构建里恒 NEEDS_CONFIG 直通。本函数从共享 generation 栈（用户级
    ``~/.topos/ai_profiles.json`` 配置）解析第一个支持 image 模态的
    profile，把 :class:`VolcengineSeedreamImageProvider` 适配成节点
    transport 契约 ``(request, api_key, output_size) -> RGBA``。

    行为约定：

    - 密钥按 ``credential_ref`` 惰性读取（每次求值时经
      HybridCredentialStore，用户改 key 即刻生效，无需重启）；
    - 无 image profile / 无凭据 → 返回 False 且不安装：节点保持可见的
      NEEDS_CONFIG 直通（与无 key 时的既有语义一致，只是原因更明确）；
    - provider 失败以异常冒泡 → ``CloudImageGenJob.run`` 捕获并转为
      FAILED 状态（可见降级，非崩溃）；
    - transport 在短生命周期 worker 线程上被调用（见
      ``set_generation_transport`` 线程契约），本函数无 Qt 依赖。

    Args:
        config: 覆盖用户配置（测试注入）；默认 ``load_user_ai_config()``。
        credential_store: 覆盖凭据存储（测试注入）；默认
            ``make_credential_store()``。
        provider_factory: 覆盖 provider 构造器 ``(profile, store) -> provider``
            （测试注入）；默认 ``VolcengineSeedreamImageProvider``。

    Returns:
        是否成功安装（找到 image profile 即安装；密钥缺失由 key provider
        返回空串，job 侧呈现 NEEDS_CONFIG）。
    """
    from src.shared.ai_models.generation.provider_profiles import AiProviderProfile
    from src.shared.ai_models.generation.user_ai_config import load_user_ai_config

    if config is None:
        config = load_user_ai_config()
    profiles_raw = config.get("profiles") if isinstance(config, dict) else None
    if not isinstance(profiles_raw, list):
        logger.debug("AI Gen transport: no profiles in user config; not wired")
        return False

    profile = None
    for item in profiles_raw:
        if not isinstance(item, dict):
            continue
        try:
            candidate = AiProviderProfile.from_dict(item)
        except (KeyError, TypeError, ValueError):
            continue  # 单条损坏跳过，不拖垮整批
        if "image" in candidate.modalities:
            profile = candidate
            break
    if profile is None:
        logger.debug("AI Gen transport: no image-capable profile; not wired")
        return False

    if credential_store is None:
        from src.shared.ai_models.generation.credential_store import (
            make_credential_store,
        )
        credential_store = make_credential_store()

    def _key_provider() -> str:
        try:
            return credential_store.get_secret(profile.credential_ref) or ""
        except Exception:  # noqa: BLE001 — key 读取失败按未配置处理
            logger.warning("AI Gen transport: credential read failed")
            return ""

    if provider_factory is None:
        from src.shared.ai_models.generation.volcengine_generation_provider import (
            VolcengineSeedreamImageProvider,
        )
        provider_factory = VolcengineSeedreamImageProvider
    provider = provider_factory(profile, credential_store)

    def _transport(request: Dict[str, Any], api_key: str,
                   output_size: Any) -> "np.ndarray":
        import uuid

        from src.shared.ai_models.generation.generation_models import (
            GenerationConsent,
            GenerationModality,
            GenerationRecipe,
            GenerationRequest,
        )

        prompt = str(request.get("prompt") or "")
        if not prompt:
            raise ValueError("generation requires a prompt")
        width = int(output_size[0]) if output_size else 0
        height = int(output_size[1]) if output_size else 0
        try:
            seed = int(request.get("seed", 0))
        except (TypeError, ValueError):
            seed = 0
        gen_request = GenerationRequest(
            request_id=f"ai-gen-expand-{uuid.uuid4().hex[:12]}",
            modality=GenerationModality.IMAGE,
            prompt=prompt,
            consent=GenerationConsent(
                confirmed=True,
                provider_id=profile.profile_id,
                cost_acknowledged=True,
                upload_acknowledged=True,
                copyright_acknowledged=True,
            ),
            negative_prompt=str(request.get("negative_prompt") or ""),
            recipe=GenerationRecipe(
                recipe_id="composite.ai_gen_expand",
                modality=GenerationModality.IMAGE,
                width=width or None,
                height=height or None,
                seed=seed,
            ) if (width and height) else None,
        )
        result = provider.generate(gen_request)
        if not result.succeeded or not result.candidates:
            raise RuntimeError(
                f"generation provider failed: {result.error_code}: "
                f"{result.error_message}")
        handle = str(result.candidates[0].preview_handle or "")
        if not handle:
            raise RuntimeError("generation provider returned no image handle")
        return _decode_image_to_rgba(handle, (width, height))

    set_generation_transport(_transport, _key_provider)
    logger.info(
        "AI Gen transport wired via profile %r (credential_ref=%s)",
        profile.profile_id, profile.credential_ref)
    return True


class AIGenExpandNode(Node):
    """Generative expand/fill backed by a cloud provider."""

    node_type = "ai_gen_expand"
    node_title = "AI Gen Expand"
    i18n_title_key = "nodes.ai_gen_expand.title"
    header_color = "#9B59B6"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "ai generate", "ai expand", "outpaint", "generative fill",
        "AI 生成扩展", "扩图", "生成式填充", "AI 扩展",
    ]
    DESCRIPTION = "生成式扩图/填充（云端 provider，静帧；无密钥时可见 NEEDS_CONFIG）"
    TAGS = ["ai", "generative", "outpaint"]

    PARAM_DEFS = [
        ParamDef("prompt", "str", "", group="Prompt"),
        ParamDef("negative_prompt", "str", "", group="Prompt"),
        ParamDef("model", "str", "default", group="Prompt"),
        ParamDef("seed", "int", 0, min_val=0, max_val=2147483647, step=1,
                 group="Prompt", animated=True),
        ParamDef("strength", "float", 1.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Prompt", animated=True),
        ParamDef("expand_left", "float", 0.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Expand", animated=True),
        ParamDef("expand_top", "float", 0.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Expand", animated=True),
        ParamDef("expand_right", "float", 0.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Expand", animated=True),
        ParamDef("expand_bottom", "float", 0.0, min_val=0.0, max_val=1.0,
                 step=0.01, group="Expand", animated=True),
        ParamDef("output_width", "int", 0, min_val=0, max_val=8192,
                 step=1, group="Expand", hidden=True),
        ParamDef("output_height", "int", 0, min_val=0, max_val=8192,
                 step=1, group="Expand", hidden=True),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [
            Port("image", "input", "image"),
            Port("mask", "input", "image"),
        ]
        self.outputs = [Port("image", "output", "image")]
        self.parameters = {
            "prompt": "",
            "negative_prompt": "",
            "model": "default",
            "seed": 0,
            "strength": 1.0,
            "expand_left": 0.0,
            "expand_top": 0.0,
            "expand_right": 0.0,
            "expand_bottom": 0.0,
            "output_width": 0,
            "output_height": 0,
        }
        # Last job outcome, surfaced for the properties panel/UI state.
        self.last_state: str = "QUEUED"
        self.state_detail: str = ""

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        return {"image": output_roi, "mask": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return 0

    # ── Job assembly (unit-testable without GL) ────────────────────────
    def _output_size(self, image: TextureResult) -> Tuple[int, int]:
        """Requested output resolution (0 → follow the input frame)."""
        width = int(self.parameters.get("output_width", 0)) \
            or int(image.width)
        height = int(self.parameters.get("output_height", 0)) \
            or int(image.height)
        return width, height

    def _effective_mask(self, width: int, height: int,
                        mask_pixels: Optional["np.ndarray"]
                        ) -> Optional["np.ndarray"]:
        """Resolve the generation mask (1.0 = pixels to GENERATE).

        A read-back connected mask (red channel) wins over the
        ``expand_*`` border mask; with neither, the request asks for a
        full-frame generation.
        """
        if mask_pixels is not None and mask_pixels.ndim >= 2 \
                and mask_pixels.shape[0] == height \
                and mask_pixels.shape[1] == width:
            return np.ascontiguousarray(
                mask_pixels[..., 0]).astype(np.float32)
        if (float(self.parameters.get("expand_left", 0.0))
                or float(self.parameters.get("expand_top", 0.0))
                or float(self.parameters.get("expand_right", 0.0))
                or float(self.parameters.get("expand_bottom", 0.0))):
            from src.shared.ai_models.generation.image_gen_job import (
                border_expand_mask,
            )
            return border_expand_mask(
                width, height,
                float(self.parameters.get("expand_left", 0.0)),
                float(self.parameters.get("expand_top", 0.0)),
                float(self.parameters.get("expand_right", 0.0)),
                float(self.parameters.get("expand_bottom", 0.0)))
        return None

    @staticmethod
    def _input_identity(image: TextureResult) -> "np.ndarray":
        """Tiny deterministic identity array standing in for the frame.

        Built from the upstream texture id / size / revision — the
        project's content-identity convention — so the cache key changes
        whenever the upstream changes without hashing pixel content.
        """
        return np.array(
            [[[float(image.texture_id), float(image.width),
               float(image.height), float(image.revision)]]],
            dtype=np.float32)

    def _mask_key_material(self,
                           mask: Optional[TextureResult]) -> Optional["np.ndarray"]:
        """Cache-key material for the connected mask (identity array)."""
        if mask is None:
            return None
        return np.array(
            [[[float(mask.texture_id), float(mask.width),
               float(mask.height), float(mask.revision)]]],
            dtype=np.float32)

    def _border_key_material(self) -> Optional["np.ndarray"]:
        """Cache-key material for the border mask (expand ratios).

        ``border_expand_mask`` is a pure function of the four ratios and
        the output size (which participates in the key separately), so
        the tiny ratio array is an equivalent — and far cheaper — key
        stand-in than the full HxW mask bytes.
        """
        ratios = (
            float(self.parameters.get("expand_left", 0.0)),
            float(self.parameters.get("expand_top", 0.0)),
            float(self.parameters.get("expand_right", 0.0)),
            float(self.parameters.get("expand_bottom", 0.0)),
        )
        if not any(ratios):
            return None
        return np.array([[list(ratios)]], dtype=np.float32)

    def _build_job(self, image: TextureResult,
                   mask_pixels: Optional["np.ndarray"],
                   input_pixels: Optional["np.ndarray"],
                   revision_signature: str,
                   mask_key_material: Optional["np.ndarray"] = None) -> Any:
        """Assemble the CloudImageGenJob for the current parameters.

        AUD-6309: the cache key is derived from cheap identity material —
        the upstream texture id/size/revision for the image, and either
        the connected mask's identity or the border ratios for the mask
        (``mask_key_material``; when absent the resolved mask itself is
        hashed, preserving the historical direct-call behaviour).  The
        previous scheme hashed the full read-back frame (~132MB at 4K) on
        every evaluation, cache hits included.

        ``input_pixels`` / ``revision_signature`` are retained for
        call-site compatibility.  The request itself never carried the
        input frame (the shared job schema has no ``image`` field — see
        ``_composite_masked`` for the outside-mask guarantee), and the
        key now uses the identity signature regardless.

        ``mask_pixels`` (the read-back mask, red channel) wins over the
        ``expand_*`` border mask (both mark pixels to generate); with
        neither, the request asks for a full-frame generation.
        """
        from src.shared.ai_models.generation.image_gen_job import (
            CloudImageGenJob,
        )
        width, height = self._output_size(image)
        real_mask = self._effective_mask(width, height, mask_pixels)
        # AUD-6312: read the injected stack as one consistent snapshot.
        transport, key_provider, cost_confirm = _generation_stack()
        # The job hashes its constructor arguments into the cache key, so
        # key on the identity material and swap the real mask in as the
        # request payload afterwards.
        key_mask = (mask_key_material if mask_key_material is not None
                    else real_mask)
        job = CloudImageGenJob(
            prompt=str(self.parameters.get("prompt", "")),
            negative=str(self.parameters.get("negative_prompt", "")),
            model=str(self.parameters.get("model", "default")),
            seed=int(self.parameters.get("seed", 0)),
            strength=float(self.parameters.get("strength", 1.0)),
            mask=key_mask,
            image=self._input_identity(image),
            cache_store=get_disk_cache_store(),
            transport=transport,
            api_key_provider=key_provider,
            cost_confirm=cost_confirm,
            output_size=(width, height),
        )
        job.mask = real_mask
        return job

    # ── Execution ─────────────────────────────────────────────────────
    def _run_job_bounded(self, job: Any,
                         count: int = 1) -> Tuple[str, Optional["np.ndarray"]]:
        """Execute ``job.run`` off the render thread with a hard timeout.

        AUD-6307: the transport call is a cloud round trip.  Running it
        inline stalled the whole render queue for the duration — and
        forever on a hung connection, because the transport had no
        timeout.  The job now executes on a short-lived worker thread and
        the render thread waits with a bounded timeout; on expiry the
        node degrades visibly (FAILED + passthrough) and the queue keeps
        moving.  The abandoned daemon worker's eventual cache write is
        idempotent (same key, same content).

        AUD-6308: ``cost_confirm`` is invoked by ``job.run`` before the
        network call, so it now runs on the worker thread — never on the
        render thread.  Interactive confirmers must marshal to the UI
        thread themselves (see ``set_generation_transport``).
        """
        outcome: Dict[str, Any] = {}

        def _worker() -> None:
            try:
                outcome["result"] = job.run(count=count)
            except Exception as exc:  # noqa: BLE001 — surfaced as FAILED
                outcome["error"] = exc

        thread = threading.Thread(target=_worker,
                                  name="ai-gen-expand-job", daemon=True)
        thread.start()
        thread.join(_GENERATION_TIMEOUT_S)
        if thread.is_alive():
            job.state.transition(
                "FAILED",
                f"generation timed out after {int(_GENERATION_TIMEOUT_S)}s")
            return "FAILED", None
        if "error" in outcome:
            exc = outcome["error"]
            job.state.transition("FAILED", f"{type(exc).__name__}: {exc}")
            logger.warning("AI Gen job raised %s: %s",
                           type(exc).__name__, exc)
            return "FAILED", None
        return outcome.get("result", ("FAILED", None))

    @staticmethod
    def _composite_masked(input_pixels: Optional["np.ndarray"],
                          rgba: "np.ndarray",
                          mask_array: Optional["np.ndarray"]
                          ) -> Optional["np.ndarray"]:
        """Blend the generated frame with the input inside the mask.

        AUD-6306: the generation request carries only the mask (the
        shared CloudImageGenJob schema has no image field), so providers
        cannot composite the request context themselves — despite the old
        comment claiming they did.  Outside the mask the input pixels
        win, the same guarantee the evaluator's ``_read_ai_cache_result``
        applies to cached LaMa results.  Returns None when the guarantee
        cannot be honored (input unavailable or shape mismatch); callers
        must then degrade visibly instead of replacing the frame.
        """
        if mask_array is None:
            return rgba
        if (input_pixels is None
                or input_pixels.shape[:2] != rgba.shape[:2]
                or mask_array.shape[:2] != rgba.shape[:2]):
            return None
        generated = np.asarray(rgba, dtype=np.float32)
        if generated.ndim != 3 or generated.shape[2] != 4 \
                or input_pixels.ndim != 3:
            return None
        m = np.clip(mask_array, 0.0, 1.0)[:, :, np.newaxis]
        return (input_pixels * (1.0 - m) + generated * m).astype(np.float32)

    def _composited_entry(self, key: str) -> str:
        """Disk-cache entry for the node's composited result."""
        return f"{_COMPOSITED_ENTRY_PREFIX}{key}"

    def _load_composited(self, key: str) -> Optional["np.ndarray"]:
        store = get_disk_cache_store()
        try:
            return store.load(self._composited_entry(key))
        except Exception as exc:  # cache problems never fail the node
            logger.warning("composited cache read failed: %s", exc)
            return None

    def _store_composited(self, key: str, final: "np.ndarray") -> None:
        store = get_disk_cache_store()
        try:
            store.store(self._composited_entry(key), final,
                        {"node_type": "ai_gen_expand",
                         "prompt": str(self.parameters.get("prompt", ""))[:120],
                         "seed": int(self.parameters.get("seed", 0)),
                         "composited": True})
        except Exception as exc:  # cache problems never fail the node
            logger.warning("composited cache write failed: %s", exc)

    def _upload_result(self, rgba: "np.ndarray", image: TextureResult,
                       ctx: Any, resource_pool: Any) -> Optional[Dict[str, TextureResult]]:
        """Upload a generated frame as a tracked GPU texture.

        AUD-6310: the bare ``ctx.texture`` allocation lived outside the
        resource pool / framebuffer-manager tracking system — no release
        path could find it (VRAM churn per evaluation) and downstream
        id-based lookups silently degraded.  Pool acquisition puts the
        texture into the budget / deferred-release system.  The bare
        allocation remains only as a last-resort fallback for callers
        without a pool (tests / exotic hosts).  Returns None when the
        upload is impossible; callers pass the input through.
        """
        try:
            h, w = int(rgba.shape[0]), int(rgba.shape[1])
            data = np.ascontiguousarray(np.flipud(rgba))
            if resource_pool is not None:
                tex = resource_pool.acquire_texture(
                    w, h, components=4, dtype="f4")
                tex.write(data.tobytes())
            else:
                gl = getattr(ctx, "gl", ctx)
                tex = gl.texture((w, h), 4, data=data.tobytes(), dtype="f4")
            return {"image": TextureResult(
                texture_id=tex.glo, width=w, height=h,
                format=image.format,
                bbox=image.bbox,
                alpha_premultiplied=image.alpha_premultiplied,
                color_space=image.color_space,
            )}
        except Exception as exc:  # noqa: BLE001 — visible passthrough below
            logger.warning("AI Gen upload failed: %s", exc)
            return None

    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}
        mask = inputs.get("mask")
        mask = mask if (mask is not None and mask.is_valid) else None

        # AUD-6312: consistent snapshot — not a bare global read.
        transport, _, _ = _generation_stack()
        if transport is None:
            self.last_state = "NEEDS_CONFIG"
            self.state_detail = "no generation transport configured"
            self.error_message = "AI Gen: NEEDS_CONFIG — no transport/key"
            return {"image": image}

        # AUD-6309: identity-keyed job — no full-frame readback and no
        # full-pixel SHA-256 on the cache-hit path.  Pixels are read back
        # only when a generation or the outside-mask composite needs them.
        mask_key = self._mask_key_material(mask)
        if mask_key is None:
            mask_key = self._border_key_material()
        job = self._build_job(image, None, None,
                              f"rev{int(image.revision)}",
                              mask_key_material=mask_key)

        if mask_key is None:
            # Full-frame generation: the provider output IS the final
            # frame, so the job's own content-addressed cache serves the
            # fast path (its internal try_cache runs before any network).
            state, rgba = self._run_job_bounded(job)
            self.last_state = state
            self.state_detail = job.state.detail
            if state != "DONE" or rgba is None:
                self.error_message = f"AI Gen: {state} — {job.state.detail}"
                # Visible degradation: keep the ungenerated input flowing.
                return {"image": image}
            self.error_message = ""
            uploaded = self._upload_result(rgba, image, ctx, resource_pool)
            return uploaded if uploaded is not None else {"image": image}

        # Masked fill: try this node's composited cache entry first — a
        # hit costs zero readback (the entry stores the fully blended
        # frame, keyed by the same identity material).
        final = self._load_composited(job.key)
        if final is not None:
            self.last_state = "DONE"
            self.state_detail = "composited cache hit"
            self.error_message = ""
            uploaded = self._upload_result(final, image, ctx, resource_pool)
            if uploaded is not None:
                return uploaded
            return {"image": image}

        # Miss: read back the pixels now — both the request mask and the
        # outside-mask composite below need them.
        from ._gpu_readback import read_rgba
        input_pixels = read_rgba(image, ctx, fb_manager)
        mask_pixels = (read_rgba(mask, ctx, fb_manager)
                       if mask is not None else None)
        width, height = job.output_size
        real_mask = self._effective_mask(width, height, mask_pixels)
        if mask is not None and real_mask is None:
            # A connected mask exists but is unusable (readback failed or
            # size mismatch) and no border mask can stand in: replacing
            # the whole frame would silently discard the user's mask
            # semantics — degrade visibly instead (AUD-6306/6314).
            logger.warning(
                "AI Gen: connected mask unusable (mask %dx%d vs output "
                "%dx%d); passing input through",
                int(mask.width), int(mask.height), int(width), int(height))
            self.last_state = "FAILED"
            self.state_detail = "connected mask unusable"
            self.error_message = "AI Gen: FAILED — connected mask unusable"
            return {"image": image}
        if mask is not None and mask_pixels is None and real_mask is not None:
            logger.warning(
                "AI Gen: connected mask readback failed; falling back to "
                "the expand_* border mask")
        # The request payload carries the real mask; the cache key stays
        # identity-based (see _build_job).
        job.mask = real_mask

        state, rgba = self._run_job_bounded(job)
        self.last_state = state
        self.state_detail = job.state.detail
        if state != "DONE" or rgba is None:
            self.error_message = f"AI Gen: {state} — {job.state.detail}"
            # Visible degradation: keep the ungenerated input flowing.
            return {"image": image}
        self.error_message = ""

        # AUD-6306: enforce the outside-mask guarantee node-side.
        final = self._composite_masked(input_pixels, rgba, real_mask)
        if final is None:
            self.last_state = "FAILED"
            self.state_detail = \
                "cannot honor mask (input readback/shape mismatch)"
            self.error_message = ("AI Gen: FAILED — input frame unavailable "
                                  "for masked composite")
            return {"image": image}
        self._store_composited(job.key, final)

        uploaded = self._upload_result(final, image, ctx, resource_pool)
        return uploaded if uploaded is not None else {"image": image}
