# Face Track

Face landmark container + passthrough output. Detections persist in the project as {frame: [(x, y, confidence)...]} JSON — reopening never re-runs detection. The detector is injected (ONNX via model_manager, never silently downloaded; without one the node shows a visible NEEDS_DETECTOR state).

Landmarks feed expressions/inspection; the skin mask is generated geometrically by face_workflow's build_skin_mask (cheek/forehead/nose polygons, eye/brow/lip exclusions, feather) and feeds Face Smooth.

Confidence gates occlusion/profile degradation; use Face Index for multi-face scenes.
