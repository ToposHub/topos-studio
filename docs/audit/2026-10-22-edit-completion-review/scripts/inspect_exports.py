"""Inspect real GUI deliverables without relying on task success status."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import subprocess
import numpy as np

folder = Path('/tmp/topos-round7-gui/exports')
output = {}
for path in sorted(folder.glob('*.mp4')):
    metadata = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(path)]))
    raw = subprocess.check_output([
        'ffmpeg', '-v', 'error', '-i', str(path), '-vn', '-f', 'f32le', '-acodec',
        'pcm_f32le', '-ac', '2', '-ar', '44100', '-'])
    pcm = np.frombuffer(raw, dtype=np.float32)
    output[path.name] = {
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'bytes': path.stat().st_size, 'ffprobe': metadata,
        'decoded_audio': {'interleaved_samples': int(pcm.size),
                          'peak': float(np.max(np.abs(pcm))) if pcm.size else None,
                          'nonzero_samples': int(np.count_nonzero(pcm)),
                          'rms': float(np.sqrt(np.mean(pcm**2))) if pcm.size else None}}
output['image_sequence_files'] = [p.name for p in sorted(folder.glob('*.png'))]
print(json.dumps(output, ensure_ascii=False, indent=2))
