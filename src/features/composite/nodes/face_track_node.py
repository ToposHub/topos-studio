"""Face Track node (C3) — landmark detection container + passthrough.

MVP: the node OWNS serialized landmark data ({frame: [(x, y, conf)...]}
JSON — savable so a project reopens without re-running detection) and
passes its input through unchanged.  Detection itself is injected via
:data:`set_face_detector` (production wires the ONNX detector through
model_manager — never downloaded silently; tests inject synthetic
landmarks).  Landmarks feed expressions/inspection; the skin mask is
produced by :mod:`src.features.composite.face_workflow` at the smooth
node.
"""

import logging
from typing import Any, Dict, Optional

from .base_node import Node, ParamDef, Port, TextureResult

if __name__ != "__main__":
    from src.features.composite.core.format import BBox

logger = logging.getLogger(__name__)

# Process-wide detector: (frame, image_array_or_None) ->
# ([(x, y), ...], confidence) or None.  Injected; not part of the graph.
#
# Threading contract (AUD-6706): the detector is written once during
# initialization wiring (production: model_manager startup; tests: fixture
# setup) and is never swapped while a graph evaluation may concurrently
# run a detection.  Readers go through :func:`current_face_detector`,
# which returns an immutable reference snapshot (a single atomic read of
# the module global) for their whole detection sequence.
FaceDetector = Any
_DETECTOR: Optional[FaceDetector] = None


def set_face_detector(detector: Optional[FaceDetector]) -> None:
    """Install the process-wide detector (initialization time only).

    See the module-level threading contract (AUD-6706): do NOT swap the
    detector while a graph evaluation may concurrently run
    :meth:`FaceTrackNode.detect_at`.
    """
    global _DETECTOR
    _DETECTOR = detector


def current_face_detector() -> Optional[FaceDetector]:
    """Snapshot read of the injected detector (AUD-6706).

    Returns the reference installed at initialization time (or ``None``).
    Callers use the returned reference for their whole detection
    sequence instead of re-reading the module global per call.
    """
    return _DETECTOR


class FaceTrackNode(Node):
    """Landmark data container with passthrough output."""

    node_type = "face_track"
    node_title = "Face Track"
    i18n_title_key = "nodes.face_track.title"
    header_color = "#9B59B6"
    IS_PLACEHOLDER = False

    SEARCH_ALIASES = [
        "face track", "face landmark", "face detect",
        "人脸跟踪", "人脸关键点", "人脸检测",
    ]
    DESCRIPTION = "人脸关键点容器（可序列化，保存后无需重跑）+ 直通输出"
    TAGS = ["ai", "tracking", "face"]

    PARAM_DEFS = [
        # Serialized landmark tracks — durable, project-saved data.
        ParamDef("landmarks", "str", "{}", group="Face", hidden=True),
        ParamDef("face_index", "int", 0, min_val=0, max_val=16, step=1,
                 group="Face"),
        ParamDef("rerun_on_evaluate", "bool", False, group="Face"),
    ]

    def __init__(self, node_id: Optional[str] = None) -> None:
        super().__init__(node_id)
        self.inputs = [Port("image", "input", "image")]
        self.outputs = [Port("result", "output", "image")]
        self.parameters = {
            "landmarks": "{}",
            "face_index": 0,
            "rerun_on_evaluate": False,
        }
        self.detection_state = "IDLE"

    @classmethod
    def params_require_frame_cache(cls, params: Dict[str, Any]) -> bool:
        return True

    def compute_output_bbox(self, input_bboxes: Dict[str, "BBox"],
                            params: Dict[str, Any]) -> Optional["BBox"]:
        return input_bboxes.get("image")

    def compute_input_roi(self, output_roi: "BBox", params: Dict[str, Any],
                          input_bboxes: Optional[Dict[str, "BBox"]] = None
                          ) -> Dict[str, "BBox"]:
        return {"image": output_roi}

    def requires_roi_expansion(self, params: Dict[str, Any]) -> int:
        return 0

    # ── Landmark API ───────────────────────────────────────────────────
    def set_landmarks(self, tracks: Dict[int, Any],
                      *, silent: bool = False) -> None:
        """Store landmark tracks as the serialized ``landmarks`` parameter.

        ``silent=True`` is for evaluation-time writes (``rerun_on_evaluate``
        inside ``_do_evaluate``): the value lands in ``parameters`` so it
        still serializes with the project, but without the dirty/revision/
        state-change side effects of ``set_parameter`` — those would
        self-invalidate the cache entry currently being produced and fire
        the state callback from the render thread (AUD-6705).  UI-initiated
        detection keeps the default full ``set_parameter`` semantics.
        """
        from ..face_workflow import serialize_landmarks
        serialized = serialize_landmarks(tracks)
        if silent:
            self._set_parameter_silent("landmarks", serialized)
        else:
            self.set_parameter("landmarks", serialized)

    def get_landmarks(self) -> Dict[int, Any]:
        from ..face_workflow import deserialize_landmarks
        return deserialize_landmarks(
            str(self.parameters.get("landmarks", "{}")))

    def clear_landmarks(self) -> None:
        self.set_parameter("landmarks", "{}")
        self.detection_state = "IDLE"

    def detect_at(self, frame: int, image_array=None,
                  *, silent: bool = False) -> bool:
        """Run the injected detector for one frame; stores the result.

        ``silent`` is forwarded to :meth:`set_landmarks` — see AUD-6705:
        the render-thread ``rerun_on_evaluate`` path must not bump
        revisions / dirty the node mid-evaluation.
        """
        # AUD-6706: snapshot read — one reference for the whole sequence.
        detector = current_face_detector()
        if detector is None:
            self.detection_state = "NEEDS_DETECTOR"
            return False
        try:
            result = detector(int(frame), image_array)
        except Exception as exc:
            logger.warning("face detector failed: %s", exc)
            self.detection_state = "FAILED"
            return False
        if not result:
            self.detection_state = "NO_FACE"
            return False
        points, confidence = result
        tracks = self.get_landmarks()
        tracks[int(frame)] = [(float(x), float(y)) for x, y in points]
        self.set_landmarks(tracks, silent=silent)
        self.detection_state = "OK"
        return True

    # ── Evaluation: passthrough (+ optional detector call) ─────────────
    def _do_evaluate(self, inputs: Dict[str, TextureResult], ctx: Any,
                     fb_manager: Any, resource_pool: Any = None,
                     render_mode: str = "preview") -> Dict[str, TextureResult]:
        image = inputs.get("image")
        if image is None or not image.is_valid:
            return {}
        if (self.parameters.get("rerun_on_evaluate")
                and current_face_detector() is not None and ctx is not None):
            # AUD-6705: silent write — no set_parameter side effects
            # (self-dirty + revision bumps + state callback) from the
            # render thread mid-evaluation.
            self.detect_at(int(getattr(ctx, "frame_number", 0) or 0),
                           silent=True)
        return {"result": image}
