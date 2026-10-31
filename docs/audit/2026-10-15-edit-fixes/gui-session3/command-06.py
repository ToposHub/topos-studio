# Correct the audit fixture's combo lookup: its visible text contains ' fps'.
record('settings_fixture_lookup',previous_requested_text='50',actual_fps=str(tv._timeline.fps))
