# Deflicker

Temporal luminance stabilisation: the windowed percentile (median by default) of each frame's luma plane is the reference; the current frame is corrected with a per-pixel gain (luma-only mode preserves chroma). Fixes brightness flicker in timelapses, old footage and screen recordings.

## Behaviour (important)

- **Causal (trailing) window**: the N-frame window ending at the current frame fills naturally during sequential playback/export.
- **Jump / cold-start degradation**: with an incomplete window the node passes the input through and shows a warming message on the node — it never fakes a full window. The first frame after reopening a project is warming passthrough by design.
- The buffer is runtime state: not serialized, not undoable, not part of the graph schema.

## Parameters

- **Window**: 3/5/7/9 frames.
- **Percentile**: 0.5 = median; higher values are more conservative.
- **Luma Only**: on by default — corrects luma ratio only, keeping chroma stable.
- **Mode**: per-pixel / global gain (single scalar gain fallback).
