"""Build the diagnostic host with production Swift sources, outside either app."""
from pathlib import Path
import plistlib
import subprocess

SOURCE = Path('/Users/heng/Documents/vscode/topos-player')
HERE = Path(__file__).resolve().parent
BUNDLE = Path('/tmp/topos-player-comparison-20261123.app')
(BUNDLE / 'Contents/MacOS').mkdir(parents=True, exist_ok=True)
frameworks = BUNDLE / 'Contents/Frameworks'
if not frameworks.exists():
    frameworks.symlink_to(SOURCE / 'build/ToposPlayer.app/Contents/Frameworks')
(BUNDLE / 'Contents/Info.plist').write_bytes(plistlib.dumps({
    'CFBundleIdentifier': 'com.topos.diagnostics.comparison20261123',
    'CFBundleExecutable': 'comparison', 'CFBundleName': 'Topos Playback Diagnostic',
    'CFBundlePackageType': 'APPL', 'NSHighResolutionCapable': True}))
mpv = (SOURCE / 'Sources/ToposMPVKit/MPVGLView.swift').read_text()
mpv = mpv.replace('openGLContext?.flushBuffer()',
                  'openGLContext?.flushBuffer()\n        playbackDiagnosticDidDraw()')
(HERE / 'MPVGLView.instrumented.swift').write_text(mpv)
metal = (SOURCE / 'Sources/Shared/ToposMetalVideoView.swift').read_text()
metal = metal.replace('try? device.makeLibrary(source: Self.shaderSource, options: nil)',
                      'playbackDiagnosticMakeLibrary(device, Self.shaderSource)')
metal = metal.replace('commandBuffer.commit()',
                      'commandBuffer.commit()\n        playbackDiagnosticDidDraw(frame.width, frame.height)')
(HERE / 'ToposMetalVideoView.instrumented.swift').write_text(metal)
views = (SOURCE / 'Sources/Shared/PlayerViews.swift').read_text()
views = views.replace('        presentationFrames += 1',
                      '        presentationFrames += 1\n        playbackDiagnosticCPUFrame(index)')
(HERE / 'PlayerViews.instrumented.swift').write_text(views)
sources = list((SOURCE / 'Sources/ToposKit').glob('*.swift'))
sources += list((SOURCE / 'Sources/ToposMPVKit').glob('*.swift'))
sources += list((SOURCE / 'Sources/Shared').glob('*.swift'))
sources = [path for path in sources if path.name not in {
    'MPVGLView.swift', 'ToposMetalVideoView.swift', 'PlayerViews.swift'}]
sources += [HERE / 'MPVGLView.instrumented.swift',
            HERE / 'ToposMetalVideoView.instrumented.swift',
            HERE / 'PlayerViews.instrumented.swift', HERE / 'main.swift']
command = ['/usr/bin/swiftc', '-O', '-whole-module-optimization', '-module-name',
           'topos_comparison', '-I', str(SOURCE / 'cinclude'), '-Xcc',
           '-fmodule-map-file=' + str(SOURCE / 'cinclude/module.modulemap')]
command += [str(path) for path in sources] + [str(SOURCE / 'build/topos_codec.dylib')]
for framework in ['AppKit', 'CoreGraphics', 'Accelerate', 'ImageIO',
                  'UniformTypeIdentifiers', 'AVFoundation', 'CoreMedia',
                  'CoreAudio', 'Metal', 'MetalKit', 'QuickLookThumbnailing',
                  'QuickLookUI', 'MediaPlayer', 'AVKit']:
    command += ['-framework', framework]
command += ['-Xlinker', '-rpath', '-Xlinker', str(SOURCE / 'build'),
            '-o', str(BUNDLE / 'Contents/MacOS/comparison')]
with (HERE / 'swift_instrumented_build.log').open('w') as log:
    raise SystemExit(subprocess.call(command, stdout=log, stderr=subprocess.STDOUT))
