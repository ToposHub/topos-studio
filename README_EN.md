# topos-studio — Professional Image and Video Editing Suite

🌐 English · [简体中文](README.md)

**topos-studio** (pronounced /ˈtoʊ.poʊs/, approximately “TOH-pohs,” with stress on the first syllable) is a high-performance, non-destructive image and video editing suite built with **PyQt6** and **ModernGL**. Its modular architecture provides four product editions and two command-line tools. The suite includes the C11 **libtopos_codec** native media codec (`.toos` intermediate format) and supports HDR workflows, ACEScg/OCIO color management, AI-assisted tools, and cross-platform GPU backends (OpenGL as the baseline, with Metal being added incrementally). Current version: **3.0** (`pyproject.toml`).

## Project Status & Important Notices

- **Development status**: The project is roughly **50% complete**, with plenty of known bugs and unfinished features being actively fixed. Issues and pull requests are warmly welcome — any form of contribution helps, and together we can make this project better!
- **AI-assisted development**: Most of this codebase was developed with the assistance of [Zhipu AI's GLM large language model](https://open.bigmodel.cn/), while the human developer handles requirement definition, architecture design, acceptance testing, and overall integration.
- **Regarding DaVinci Resolve**: The UI layout and interaction flow of this project draw inspiration from the DaVinci Resolve user experience, but **no DaVinci Resolve code, assets, fonts, or resources were used, copied, or derived** — everything is an independent implementation built on PyQt6 / ModernGL. DaVinci Resolve is a trademark of Blackmagic Design Pty. Ltd. This project is not affiliated with, sponsored by, or endorsed by Blackmagic Design.

## Screenshots

<!-- Screenshot slot: main UI screenshot pending. Suggested path docs/image/screenshots/, e.g.:
![Topos Studio main window](docs/image/screenshots/studio-main.png)
(No screenshot assets exist in the repository yet; placeholder kept, no broken external image references)
-->

## 1. Product Editions

topos-studio builds four professional product editions from one codebase:

| Product | Feature set | Intended use |
|---|---|---|
| **Topos Studio** | Color + Edit + Composite + Deliver, with Teaching Animation | Full post-production suite |
| **Topos Color** | Color + lightweight Deliver | Dedicated grading for photographers and colorists |
| **Topos Edit** | Edit + Deliver + Color viewer/transform | Video editing |
| **Topos Composite** | Composite + Deliver + Color viewer/transform + Teaching Animation | Node-based compositing and motion graphics |

Two command-line tools are also installed with `pip install -e .`: **`toposctl`** (AI editing protocol CLI; `read`, `validate`, and `preview` use the same `ToolFacade` path as the UI and MCP) and **`topos-vault`** (media asset library CLI: AI indexing, relinking after files move, provider status).

## 2. Quick Start

```bash
# Convenience launchers (detect Python architecture, GPU, and system dependencies)
./run_topos.command        # macOS
bash run_topos.sh          # Linux
run_topos.bat              # Windows

# Or: install, then launch by product
pip install -e .
topos-studio               # or topos-color / topos-edit / topos-composite

# Or: launch a module directly
python -m apps.topos_color.main
python src/main.py --product studio   # unified entry point + product selector
```

Installation steps, Python/Qt version requirements, and CPU-architecture migration notes: [docs/getstarted/installation.md](docs/getstarted/installation.md).

## 3. Feature Highlights

- **Color:** Primary/secondary grading, curves, OCIO v2 + ACEScg color management, eight all-GPU scopes, GPU video denoising, DaVinci-style tracker, Tangent hardware panels, LUTs.
- **Edit:** Multitrack timelines (16 blend modes), subtitle/title templates, scene detection, timeline occlusion culling (measured speedups up to **5.23×**), J/K/L-aware playback engine, professional audio mixer (effects/automation/recording).
- **Composite:** **93 nodes** (tracking/keying/depth and atmosphere/paint/particles/AI/3D subsystem), projects up to 16K, node disk caches (`.toos`).
- **Deliver:** H.264/H.265/ProRes/DNxHR/TOPOS intermediate encoding, VideoToolbox/NVENC/AMF hardware encoding, batch export and render queue, SRT subtitle export, fully verified export manifests.
- **AI:** Generation/inpainting/outpainting/super-resolution, transactional AI editing (host-issued `ApprovalGrant`), explainable node contracts, unified AI skill runtime and visual workflow orchestration `[Experimental]`.
- **Teaching Animation / Motion Graphics:** Scene editor + multi-scene pages, 13 built-in presets, SVG/HTML dual render backends, Figma-style auto-layout, fail-closed AI-assisted generation.
- **Native media formats:** `.toos` images (integer/float16/RAW tiers) and TOPOS video (seven intra-frame tiers + LP + RAW), Topos RAW camera RAW codec.
- **Scripting and automation:** Python scripting API (the `topos` object), `.tglsl`/`.tai` plugin systems, ten interface locales.

Full feature lists and measured data: [docs/getstarted/features-overview.md](docs/getstarted/features-overview.md).

## 4. Documentation Navigation

**Getting started (docs/getstarted/)**

| Document | Contents |
|---|---|
| [installation.md](docs/getstarted/installation.md) | Installation and environment: dependencies, launching, architecture migration |
| [features-overview.md](docs/getstarted/features-overview.md) | Feature overview: editions, architecture, core modules, full feature list |
| [faq.md](docs/getstarted/faq.md) | FAQ: macOS / Windows / Linux platform issue tables |
| [building.md](docs/getstarted/building.md) | Building and packaging: native codec, tests, PyInstaller |
| [development.md](docs/getstarted/development.md) | Development guide: layout, plugins, workflows, and conventions |
| [bilingual-map.md](docs/getstarted/bilingual-map.md) | Chinese–English section map (bilingual equivalence checks) |

**Going deeper**

- Full documentation index: [docs/INDEX.md](docs/INDEX.md)
- Architecture volumes: [docs/architecture/](docs/architecture/) (app-products / color-rendering / composite / deliver-export / edit-timeline / shared-foundations)
- Codec whitepapers: [Topos Codec whitepaper](native/topos_codec/Topos_Codec_Whitepaper.md) ([Chinese](native/topos_codec/Topos_Codec_白皮书.md)) · [Topos Image whitepaper](native/topos_codec/Topos_Image_Whitepaper.md) ([Chinese](native/topos_codec/Topos_Image_白皮书.md))
- Plugin development: [docs/PLUGIN_DEV_GUIDE.md](docs/PLUGIN_DEV_GUIDE.md)
- Architecture decision records: `docs/adr/`, `docs/image/` (ADR-I series), `native/topos_codec/docs/` (ADR-C series)

## 5. Compatibility and Environment

**topos-studio supports Qt 6 only** (PyQt6 ≥ 6.10.2 and the versions listed in `requirements.txt`); Qt 5 is not supported (due to the QtWebEngine/Chromium minimum version). Python ≥ 3.10 (CI validates 3.10 and 3.12). A virtual environment (`.venv`) cannot be copied between CPU architectures and must be recreated when switching machines.

Version requirement details: [docs/getstarted/installation.md](docs/getstarted/installation.md); platform troubleshooting: [docs/getstarted/faq.md](docs/getstarted/faq.md).

## Project Information

- Version: **3.0** (`pyproject.toml`)
- License: **GPL-3.0** (full text in [LICENSE](LICENSE))
- Contact: [toposhub@outlook.com](mailto:toposhub@outlook.com)
- Test suite: 1,465+ test files / 22,000+ test functions and cases (recent results: `tests/edit` 8,216 passed, `composite` 5,571 passed)
- Interface languages: ten (Simplified Chinese, English, German, Spanish, French, Italian, Japanese, Korean, Brazilian Portuguese, Russian)
