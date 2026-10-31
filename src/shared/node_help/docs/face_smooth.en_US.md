# Face Smooth

Skin-restricted bilateral smoothing: the `mask` input (from build_skin_mask or any matte) scopes the effect — **pixels outside are bit-identical**; inside it reuses the validated bilateral denoise shader (edge-preserving, luma-only mode keeps chroma). `strength=0` is an exact passthrough.

- **Strength** 0-1 animatable; **Radius** is the bilateral sampling radius (also tile padding); **Luma Only** defaults on.
- A missing mask passes the input through with a visible message.
