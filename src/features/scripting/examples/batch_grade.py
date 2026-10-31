# Batch Color Grading
# Applies a consistent cinematic look to all images in a folder.

# Define the grading style to apply
def cinematic_grade(t):
    t.grade(
        exposure=-0.2,
        contrast=25,
        saturation=-10,
        highlights=-30,
        shadows=20,
        temperature=5
    )
    t.set_lift(0.0, 0.02, 0.01)
    t.set_gain(0.0, -0.02, 0.03)

# Find all images in the source directory
files = topos.list_files("/path/to/photos/", extensions=[".jpg", ".cr2", ".nef"])
topos.print(f"Found {len(files)} images")

# Process each image with the same style
results = topos.batch(
    paths=files,
    callback=cinematic_grade,
    output_dir="/output/graded/",
    format="JPEG",
    quality=95
)

topos.print(f"Successfully processed {len(results)} images")
