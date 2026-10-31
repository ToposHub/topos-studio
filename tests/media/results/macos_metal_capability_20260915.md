# macOS Metal Capability Probe

- Status: `partial`
- Generated: `2026-09-15T06:33:00.799468+00:00`
- Full-chain GPU claim: `false`
- Claim reason: `capability_probe_only; export_gpu_trail_required`

## Device

- `name`: `AMD Radeon RX 6900 XT`
- `has_unified_memory`: `False`
- `max_threads_per_group`: `<Metal.MTLSize width=1024 height=1024 depth=1024>`
- `argument_buffers_tier`: `1`
- `max_buffer_length_bytes`: `3758096384`

## Format matrix

| Format | Status | Detail |
|---|---|---|
| `NV12  8-bit` | `supported` | `` |
| `P010 10-bit` | `supported` | `` |
| `BGRA  8-bit` | `supported` | `` |
| `422 8-bit  (2vuy, 2B/px) as GBGR422` | `supported` | `` |
| `422 8-bit  (2vuy, 2B/px) as BGRG422` | `supported` | `` |
| `422 10-bit (v210, 8/3B/px) as GBGR422(8bit reinterpret)` | `supported` | `` |
| `422 10-bit (v210, 8/3B/px) as BGRG422(8bit reinterpret)` | `supported` | `` |

## Failure reasons

- `pyobjc_corevideo_route_unusable`

## Evidence boundary

This report proves capability-probe observations only. Full-chain GPU status must come from an ExportGpuTrail with decode, render, yuv_conversion, and encode_submit GPU timestamps and zero CPU pixel upload/download bytes.
