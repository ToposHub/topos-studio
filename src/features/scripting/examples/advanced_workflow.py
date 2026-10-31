# Advanced: Multi-layer grading with LUT, curves, and qualifier

topos.open("/path/to/portrait.cr2")

# --- Layer 1: Base correction ---
topos.grade(
    exposure=0.3,
    contrast=10,
    temperature=-5,
    highlights=-15,
    shadows=5
)

# Apply a custom S-curve on the master channel.
# NOTE: curve points follow the CurvesWidget convention with an INVERTED
# Y axis (UI screen coordinates): y=0 is the top (white / max output),
# y=255 is the bottom (black / min output). The identity curve is
# [(0, 255), (255, 0)]. Here y = 255 - old_forward_y.
topos.set_curves(
    master=[(0, 255), (64, 197), (128, 120), (192, 55), (255, 0)]
)

# --- Layer 2: Skin tone isolation via qualifier ---
topos.add_layer("Skin Tone")
topos.set_qualifier(
    hue_center=0.08,
    hue_width=0.15,
    hue_soft=0.05,
    sat_low=0.15,
    sat_high=0.85,
    lum_low=0.3,
    lum_high=0.9
)
topos.enable_qualifier(True)
topos.grade(saturation=8, vibrance=10)

# --- Layer 3: Cinematic LUT ---
topos.add_layer("Cinematic Look")
topos.apply_lut("cinematic.cube", intensity=0.7)

# Export in high quality TIFF
output = topos.save("~/Desktop/final_grade.tif", format="TIFF")
topos.print(f"Final export: {output}")

# Print image info
w, h = topos.get_image_size()
topos.print(f"Image size: {w}x{h}")
topos.print(f"Layers: {topos.get_layer_count()}")

# --- P1-COLOR-24: API 版本声明 + 镜头遍历 + dry-run diff ---
# 在脚本头声明 "# topos-api: 1.0" 可在主版本不兼容时拒绝执行并提示迁移。
# topos.api_version()                     -> "1.0"
# topos.list_clips()                      -> 当前时间线 clip 只读清单
# topos.clip_grade(clip_id)               -> clip 调色快照
# topos.grade_snapshot() / topos.diff_grade(before, after) -> dry-run diff
