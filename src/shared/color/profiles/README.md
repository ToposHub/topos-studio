# Bundled ICC Color Profiles

This directory holds standard ICC color profiles bundled with the app so color
management does not depend on the host OS shipping matching `.icc` files.

## Required profiles (filenames must match `_BUNDLED_PROFILE_FILES` in
## `src/features/color/core/color_manager.py`)

| Space                          | Filename                      | Source                                   |
|--------------------------------|-------------------------------|------------------------------------------|
| sRGB                           | `sRGB-IEC61966-2.1.icc`       | ICC / sRGB.com (official, royalty-free)  |
| Adobe RGB (1998)               | `AdobeRGB1998.icc`            | Adobe (redistributable per EULA)         |
| Rec. 2020                      | `Rec2020.icc`                 | ICC / color.org                          |
| DCI-P3                         | `DCI-P3.icc`                  | ICC / DCI                                |
| Display P3                     | `DisplayP3.icc`               | Apple (via macOS ColorSync)              |
| CMYK US Web Coated SWOP        | `USWebCoatedSWOP.icc`         | Adobe (redistributable per EULA)         |
| CMYK Coated FOGRA39            | `CoatedFOGRA39.icc`           | ECI / Fogra (redistributable)            |

## How to obtain / add them

Drop the matching `.icc` files into this directory. On macOS the standard
profiles are already installed and can be copied from:

```
/Library/Application Support/Adobe/Color/Profiles/Recommended/
/System/Library/ColorSync/Profiles/
```

On Linux/Windows, download from color.org / Adobe / ECI.

## Fallback behavior

If a profile file is missing here, `ColorManager.get_profile_for_space()` falls
back to:

1. The OS-installed profile of the same name (via the system profile scan).
2. The in-process sRGB profile (always available).

So the app runs correctly even before profiles are added — color management
just becomes OS-dependent instead of self-contained.
