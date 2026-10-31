# Cryptomatte Select (ID Matte)

Extract a matte for picked object ids from a Cryptomatte / OBJECT_ID AOV — pick objects for local grading or background swaps without roto.

## Input

`image`: the ID texture. Connect Scene3D's `object_id` output directly, or an ID channel read back from EXR.

## Usage

1. After connecting the ID texture, hand the scene manifest to the node (done automatically by the pick path).
2. Click pixels in the viewer to pick objects (pick API: `pick_pixel(rgba)` decodes the id → `toggle_selection(id, name)` adds/removes it from the selection set).
3. `Output Mode`:
   - **Matte** — white for selected objects, black elsewhere (feed matte_cleanup / erode_dilate for edge work, or use as a mask input anywhere).
   - **Isolate** — input × matte (original pixels kept for selected areas).

## ID Encoding

- **RGBA24 Allocator ID** (default): this project's Scene3D OBJECT_ID encode (24-bit int split across RGB/255), integer comparison.
- **Float R (Standard Cryptomatte)**: the red channel carries the bit-preserving float32 id (murmurhash3_32 → float), compared at 1e-6. Selected names are re-mapped to ids by name when the manifest changes; names that cannot be re-resolved keep their ids and are reported offline.

## Limitations

- The selection set holds at most 32 objects; standard float-pair external Cryptomatte EXR support (converter) is a later release.
- Difference from standard Cryptomatte: the texture side of this project uses 24-bit allocator integer ids while the manifest stays MurmurHash3_32 hex→name; the two meet in Float R mode.
