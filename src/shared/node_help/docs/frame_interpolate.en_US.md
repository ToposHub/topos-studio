# Frame Interpolate

Real-motion in-betweening between two frames via RIFE (ONNX): given
`frame_a`, `frame_b` and an animatable position `t` (0 = A, 1 = B), the
output is the synthesized intermediate frame — objects actually move to
their halfway positions instead of cross-fading. Pair it with time
remapping for speed changes (animate `t` from the retime fraction and
feed the two neighbouring source frames).

## Behaviour

- **Endpoints are exact**: `t = 0` / `t = 1` return the input frame
  untouched (no resample, no inference).
- **Model management**: the RIFE model is resolved through the shared
  model manager and is never downloaded silently — convert it locally
  (see `tools/convert_rife_to_onnx.py`) and register it first.
- **Graceful downgrade**: without a usable model the node falls back to
  a linear blend and reports the quality tier (`used_fallback`), so the
  graph stays renderable; the fallback is visibly reported, never hidden.
- **hd_mode** tiles large frames through the same session to bound memory
  (tiles are overlap-free; expect faint seams on extreme sizes).
- Deterministic: the same inputs and `t` always produce the same frame.

## Parameters

- **t**: interpolation position, 0..1, animatable.
- **hd_mode**: tile inference for frames larger than 1280 px.
