# Motion Graphic template library

Each direct child directory is one portable MG generator template. Copy a
whole directory to migrate or install it. The required package files are:

```text
your-template/
├── manifest.json   # identity, parameters, timing and file names
├── template.html   # self-contained HTML/CSS/Canvas animation
└── preview.gif     # required hover preview in Effects → Video
```

`manifest.json` uses `schema_version: 1`. File paths must be relative to the
template directory. The loader ignores invalid packages without preventing the
rest of the library from starting.

Templates run through the existing Motion Graphic HTML safety and rendering
pipeline. Keep external assets HTTPS-only and make each package self-contained
where possible.
