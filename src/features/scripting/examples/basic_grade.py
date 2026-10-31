# Basic Color Grading
# Opens an image, applies exposure and contrast adjustments, exports result.

topos.open("/path/to/image.jpg")

# Apply primary grading adjustments
topos.grade(
    exposure=0.5,
    contrast=15,
    saturation=10,
    highlights=-20,
    shadows=10,
    temperature=3,
    vibrance=5
)

# Fine-tune the color wheels
topos.set_lift(0.0, 0.05, 0.02)
topos.set_gain(0.0, -0.03, 0.05)

# Export the graded result
output = topos.save("/output/graded.jpg", format="JPEG", quality=95)
topos.print(f"Exported to: {output}")
