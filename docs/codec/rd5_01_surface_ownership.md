# RD5-01 surface ownership protocol

## Scope

The Topos native surface entry points write synchronously into caller-owned
`topos_plane_view` addresses. They do not create, retain, fence, or release a
Metal, IOSurface, D3D11, Vulkan, PBO, or dmabuf resource.

The platform adapter owns this lifecycle:

```text
acquire → map → native decode → unmap → GPU fence → release
```

`map` must return plane views whose geometry and stride were allocated by the
adapter. The mapped address remains valid until the native call returns. The
adapter must not recycle an in-flight surface before its fence signals.

## Python contract

`src/shared/media/frame_path.py::DecodeSurfaceLease` enforces the lifecycle and
keeps mapped views inaccessible after `unmap`. `ToposDecoder.decode_surface_lease`
brackets only the synchronous native write with `begin_decode/end_decode`; the
caller remains responsible for `unmap`, `fence`, and `release`.

`zero_copy=True` is allowed only when the mapped address is the actual
GPU/shared resource. A PBO or staging buffer is a CPU-upload path and must keep
`zero_copy=False` with a diagnostic copy reason.

## Safety invariants

1. Release before unmap/fence is rejected.
2. Mapping twice, decoding while unmapped, and unmapping while decoding are
   rejected.
3. Release is idempotent and its callback runs at most once.
4. The codec ABI receives no ownership callback and retains no pointer after
   return, so platform synchronization stays outside codec worker threads.
