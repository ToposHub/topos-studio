# RD5-03 platform surface gate

`detect_topos_surface_capability()` is the single decision point for
Windows/Linux surface routing.

| Platform/result | `zero_copy` | Meaning |
| --- | ---: | --- |
| macOS IOSurface mapped | yes | The platform supplies a mapped shared surface; renderer still owns texture import and fence. |
| Windows D3D11 shared mapped | yes | Only selected after the interop layer proves a CPU-writable mapping. |
| Windows D3D11 staging | no | A staging/readback/upload copy is visible in diagnostics. |
| Linux dmabuf mapped | yes | Only selected after the dmabuf mapping contract is supplied. |
| Linux staging | no | No dmabuf mapping is assumed from device presence alone. |
| OpenGL PBO | no | PBO is an asynchronous staging copy, not direct codec-to-texture decode. |
| CPU planar upload | no | Safe universal fallback. |

Zero-copy verdicts carry an `evidence` field (`static` / `env_override` /
`probe`) and a `zero_copy_proven` property that is true only for
`evidence="probe"`. Performance gates must check `zero_copy_proven`, never
the bare `zero_copy` flag.

The environment flags `TOPOS_D3D11_SHARED_MAPPED=1` and
`TOPOS_DMABUF_MAPPED=1` are diagnostics only (P0-06): they can never produce
`zero_copy=True`. The mapped backend is selected only when the in-process
platform probe (`_probe_windows_d3d11_shared` via
`windows_hw.capability.probe_zero_copy_capability`, `_probe_linux_dmabuf`)
confirms the mapping; otherwise routing stays on staging and the unproven
env flag is recorded in the `reason`.
