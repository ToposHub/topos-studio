# Lens Flare

Procedural flare MVP: a **ghost chain** (mirrored highlights along the centre↔light axis, spacing/decay/size configurable), a **halo ring** (configurable radius around the light) and an **anamorphic streak** (horizontal bar). Screen or add blending; `Occlusion Aware` attenuates the flare by the input alpha at the light position. Full-frame screen-space, padding 0; the draft tier halves the ghost count.

- **Light X / Y**: screen UV (may exceed the frame), animatable.
