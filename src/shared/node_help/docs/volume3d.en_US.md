# Volume 3D

Dense volume rendering (CPU raymarch MVP, front-to-back Beer–Lambert integration).

**Dependency gate (constraint 13)**: pyopenvdb is unavailable in this environment (spike result) — `.vdb` files produce an explicit dependency error instead of a crash; convert them offline with `tools/convert_vdb.py` (run it in an OpenVDB-equipped environment) into the `.npz` dense format `{"density": (D,H,W) float32}`. The GPU scene3d raymarch pass waits for the dependency decision.

- **Density Multiplier** animatable; **Step Count** halves automatically in preview; **Absorption Color** in the linear domain.
