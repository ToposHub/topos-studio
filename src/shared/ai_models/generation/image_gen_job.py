"""Cloud image-generation job runner (C1).

Module-neutral bridge between the composite graph and a cloud generation
provider (Volcengine Ark or any callable with the same contract):

* request assembly from node params (prompt/negative/strength/seed/model)
* ``expand_edges`` → automatic border mask generation for outpainting
* result caching keyed by prompt+seed+model+input-image hash — any input
  change invalidates the key, so stale results are impossible
* explicit state machine surfaced to the node/UI:
  QUEUED → UPLOADING → RUNNING → DOWNLOADING → DONE
  with NEEDS_CONFIG (no API key) and FAILED (transport/provider/content
  errors — the reason is carried on the job) as visible terminal states
* cost-confirmation hook: invoked ONCE per batch with the requested image
  count before the first call (约束 12 — keys never live here; the caller
  injects a transport that reads the key from preferences/env)

No network happens inside this module unless the caller provides a real
``transport``; tests inject a stub, which keeps the whole chain mockable.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

# Job states (kept as plain strings — they cross the graph/UI boundary).
QUEUED = "QUEUED"
UPLOADING = "UPLOADING"
RUNNING = "RUNNING"
DOWNLOADING = "DOWNLOADING"
NEEDS_CONFIG = "NEEDS_CONFIG"
FAILED = "FAILED"
DONE = "DONE"

# Transport signature: (request: dict, api_key: str, out_size: (w, h))
# -> np.ndarray RGBA float32.  Providers translate ``request`` into their
# wire format; this module stays provider-neutral.  The request carries an
# optional reserved ``"on_stage"`` callable a transport MAY invoke with
# UPLOADING/RUNNING/DOWNLOADING to drive the observable job state through
# its real phases (AUD-31610); transports that ignore it get the phases
# synthesised around the call.
Transport = Callable[[Dict[str, Any], str, Tuple[int, int]],
                     "np.ndarray"]

# Cost gate: (image_count: int, prompt: str) -> bool.  Called once per
# batch; returning False aborts before any network call.
CostConfirm = Callable[[int, str], bool]

# Chunk size for hashing array payloads straight off a contiguous view
# (~4MB keeps each hashlib update cache-friendly without ever
# materializing a second full copy of the frame).
_HASH_CHUNK_BYTES = 4 * 1024 * 1024


def border_expand_mask(width: int, height: int, left: float, top: float,
                       right: float, bottom: float) -> "np.ndarray":
    """Outpainting mask for edge expansion ratios (0..1 of the size).

    1.0 marks pixels to GENERATE (the expanded border); the original frame
    area stays 0.  Ratios are fractions of width/height on each side.
    """
    mask = np.zeros((int(height), int(width)), dtype=np.float32)
    ml = int(round(float(left) * width))
    mt = int(round(float(top) * height))
    mr = int(round(float(right) * width))
    mb = int(round(float(bottom) * height))
    if ml > 0:
        mask[:, :ml] = 1.0
    if mr > 0:
        mask[:, width - mr:] = 1.0
    if mt > 0:
        mask[:mt, :] = 1.0
    if mb > 0:
        mask[height - mb:, :] = 1.0
    return mask


def _feed_key_part(digest: "hashlib._Hash", part: Any) -> None:
    """Length-prefix and feed one cache-key part into ``digest``.

    ``part`` is either bytes (hashed as-is) or a numpy array.  Arrays are
    hashed in ~4MB chunks taken directly from a contiguous float32 view
    (AUD-31611): the previous ``astype(np.float32)`` + ``tobytes()``
    materialized two extra full copies of every frame — a 4K RGBA image
    is ~133MB and a job is rebuilt on every node param change — purely to
    feed sha256.  The digest stream is byte-identical to the old
    ``tobytes()`` form, so existing cache entries stay valid.
    """
    if isinstance(part, (bytes, bytearray)):
        digest.update(len(part).to_bytes(8, "little"))
        digest.update(bytes(part))
        return
    view = np.ascontiguousarray(part, dtype=np.float32)
    digest.update(view.nbytes.to_bytes(8, "little"))
    if view.ndim == 0:
        digest.update(view.reshape(1).view(np.uint8))
        return
    if view.shape[0] == 0:
        return
    row_nbytes = max(1, int(view[0].nbytes))
    rows_per_chunk = max(1, _HASH_CHUNK_BYTES // row_nbytes)
    for start in range(0, view.shape[0], rows_per_chunk):
        block = view[start:start + rows_per_chunk]
        # reshape/view on a contiguous slice are metadata-only: the chunk
        # feeds sha256 straight through the buffer protocol, no copy.
        digest.update(block.reshape(-1).view(np.uint8))


def cache_key(prompt: str, negative: str, model: str, seed: int,
              strength: float, image_bytes: Union[bytes, "np.ndarray"],
              mask_bytes: Union[bytes, "np.ndarray", None] = b"",
              size: str = "") -> str:
    """Content-addressed cache key for one generation result.

    ``size`` (``"WxH"``) participates in the digest so the same request at
    a different output resolution never reuses the other resolution's
    cached frame.  The image/mask parts accept either the historical
    bytes payload or the raw numpy array (hashed chunk-wise without a
    full copy — AUD-31611).
    """
    digest = hashlib.sha256()
    for part in (prompt.encode("utf-8"), negative.encode("utf-8"),
                 model.encode("utf-8"), str(int(seed)).encode(),
                 f"{float(strength):.4f}".encode()):
        digest.update(len(part).to_bytes(8, "little"))
        digest.update(part)
    _feed_key_part(digest, image_bytes)
    _feed_key_part(digest, b"" if mask_bytes is None else mask_bytes)
    size_bytes = size.encode("utf-8")
    digest.update(len(size_bytes).to_bytes(8, "little"))
    digest.update(size_bytes)
    return digest.hexdigest()[:32]


@dataclass
class GenJobState:
    """Observable job state (the node mirrors this onto its UI state)."""

    state: str = QUEUED
    detail: str = ""
    history: List[str] = field(default_factory=lambda: [QUEUED])

    def transition(self, new_state: str, detail: str = "") -> None:
        self.state = new_state
        self.detail = detail
        self.history.append(new_state)


class CloudImageGenJob:
    """One generation request with caching, state machine and cost gate."""

    # Canonical transport-phase order (AUD-31610).
    _STAGE_ORDER = (UPLOADING, RUNNING, DOWNLOADING)

    def __init__(self, prompt: str, image: "np.ndarray",
                 negative: str = "", model: str = "default",
                 seed: int = 0, strength: float = 1.0,
                 mask: Optional["np.ndarray"] = None,
                 cache_store: Any = None,
                 transport: Optional[Transport] = None,
                 api_key_provider: Optional[Callable[[], str]] = None,
                 cost_confirm: Optional[CostConfirm] = None,
                 output_size: Optional[Tuple[int, int]] = None):
        self.prompt = str(prompt)
        self.negative = str(negative)
        self.model = str(model)
        self.seed = int(seed)
        self.strength = float(strength)
        self.mask = mask
        self.image = image
        self.output_size = output_size or (
            int(image.shape[1]), int(image.shape[0]))
        self.state = GenJobState()
        self.transport = transport
        self.api_key_provider = api_key_provider
        self.cost_confirm = cost_confirm
        # Cache key computed straight off the (contiguous float32 view of
        # the) arrays — no astype/tobytes full-frame copies (AUD-31611).
        self.key = cache_key(self.prompt, self.negative, self.model,
                             self.seed, self.strength, image,
                             mask if mask is not None else b"",
                             f"{self.output_size[0]}x"
                             f"{self.output_size[1]}")
        self._cache = cache_store

    # ── Cache ──────────────────────────────────────────────────────────
    def _cache_entry(self) -> str:
        return f"gen{self.key}"

    def _stage_index(self) -> int:
        """Index of the current state in the canonical phase order (-1 if before/after)."""
        try:
            return self._STAGE_ORDER.index(self.state.state)
        except ValueError:
            return -1

    def _advance_stage_to(self, stage: str) -> None:
        """Transition forward to ``stage``, synthesising skipped phases.

        Keeps the QUEUED→UPLOADING→RUNNING→DOWNLOADING history monotone
        and complete for transports that never report stages, while a
        reporting transport's transitions simply advance the cursor.
        """
        target = self._STAGE_ORDER.index(stage)
        current = self._stage_index()
        while current < target:
            current += 1
            self.state.transition(self._STAGE_ORDER[current])

    def _make_stage_reporter(self) -> Callable[[str], None]:
        """Optional progress hook handed to the transport (AUD-31610).

        Placed on the request dict under the reserved ``"on_stage"`` key.
        Transports that know their internal phases call
        ``request["on_stage"](RUNNING)`` / ``(DOWNLOADING)`` so the
        observable job state tracks the real upload/run/download phases;
        opaque transports simply ignore it and the phases advance after
        the transport returns.  Unknown or backwards stages are ignored.
        """
        def report(stage: str) -> None:
            if stage not in self._STAGE_ORDER:
                return
            if self._stage_index() < self._STAGE_ORDER.index(stage):
                self._advance_stage_to(stage)
        return report

    def try_cache(self) -> Optional["np.ndarray"]:
        if self._cache is None:
            return None
        try:
            return self._cache.load(self._cache_entry())
        except Exception as exc:  # cache problems never fail the job
            logger.warning("generation cache read failed: %s", exc)
            return None

    def store_cache(self, rgba: "np.ndarray") -> None:
        if self._cache is None:
            return
        try:
            self._cache.store(self._cache_entry(), rgba,
                              {"node_type": "ai_gen_expand",
                               "prompt": self.prompt[:120],
                               "seed": self.seed})
        except Exception as exc:
            logger.warning("generation cache write failed: %s", exc)

    # ── Execution ──────────────────────────────────────────────────────
    def run(self, count: int = 1) -> Tuple[str, Optional["np.ndarray"]]:
        """Execute the job; returns (state, image-or-None).

        ``state`` is DONE on success, NEEDS_CONFIG without a key, FAILED
        with ``self.state.detail`` carrying the reason.
        """
        # 1. Cache hit — no network, no cost gate.
        cached = self.try_cache()
        if cached is not None:
            self.state.transition(DONE, "cache hit")
            return DONE, cached

        # 2. Key resolution (from the caller — never stored here).
        api_key = (self.api_key_provider() or ""
                   if self.api_key_provider else "")
        if not api_key:
            self.state.transition(
                NEEDS_CONFIG,
                "no API key configured — set it in preferences")
            return NEEDS_CONFIG, None

        # 3. Cost confirmation — once per batch, before any network call.
        if self.cost_confirm is not None and not self.cost_confirm(
                int(count), self.prompt):
            self.state.transition(FAILED, "cost confirmation declined")
            return FAILED, None

        if self.transport is None:
            self.state.transition(FAILED, "no transport configured")
            return FAILED, None

        # 4. Wire the request through the state machine.
        request = {
            "prompt": self.prompt,
            "negative_prompt": self.negative,
            "model": self.model,
            "seed": self.seed,
            "strength": self.strength,
            "mask": self.mask,
        }
        # Observable state must track the REAL transport phase (AUD-31610):
        # the three states used to be set in one jump BEFORE the network
        # call, so a job sat in DOWNLOADING for the whole (tens of seconds)
        # cloud round trip and failures were recorded as
        # UPLOADING→RUNNING→DOWNLOADING→FAILED.  UPLOADING is entered now;
        # RUNNING/DOWNLOADING advance either via the optional transport
        # stage callback below, or (for opaque transports) after the call
        # returns, so the failure path records the phase it died in.
        self.state.transition(UPLOADING)
        request["on_stage"] = self._make_stage_reporter()
        try:
            rgba = self.transport(request, api_key, self.output_size)
            self._advance_stage_to(DOWNLOADING)
        except Exception as exc:
            self.state.transition(FAILED, str(exc))
            return FAILED, None

        if rgba is None:
            self.state.transition(FAILED, "provider returned no image")
            return FAILED, None
        self.store_cache(rgba)
        self.state.transition(DONE)
        return DONE, rgba


def expand_request_params(prompt: str, negative: str, model: str,
                          seed: int, strength: float,
                          expand_edges: Tuple[float, float, float, float],
                          width: int, height: int) -> Dict[str, Any]:
    """Assemble the shared request dict for an outpainting node.

    Returns the request plus the auto-generated border mask (1.0 = the
    pixels the provider should generate).
    """
    left, top, right, bottom = expand_edges
    mask = border_expand_mask(width, height, left, top, right, bottom)
    request = {
        "prompt": str(prompt),
        "negative_prompt": str(negative),
        "model": str(model),
        "seed": int(seed),
        "strength": float(strength),
        "mask": mask,
    }
    return request


def dumps_state(state: GenJobState) -> str:
    """JSON snapshot for diagnostics/UI (history excluded from caching)."""
    return json.dumps({"state": state.state, "detail": state.detail})
