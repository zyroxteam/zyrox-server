"""ZYROX MUSIC ENHANCE — server-side audio engine (ffmpeg + numpy)."""
import os
import subprocess
import wave

import numpy as np

SR = 44100
MAX_SECONDS = 900          # hard cap: 15 minutes
FFMPEG = os.environ.get('FFMPEG_PATH', 'ffmpeg')
NF = 'loudnorm=I=-14:TP=-1:LRA=11,alimiter=limit=0.95'   # normalize + safety limiter

BANDS = [60, 170, 310, 600, 1000, 3000, 6000, 12000]

TOOLS = {
    'bass':   {'label': 'Bass Boosted',   'prefix': 'ZYROX-bass-boosted'},
    'eq':     {'label': 'Equalizer',      'prefix': 'ZYROX-eq'},
    'reverb': {'label': 'Slow Reverb',    'prefix': 'ZYROX-slow-reverb'},
    'cut':    {'label': 'Cut Audio',      'prefix': 'ZYROX-cut'},
    'v2a':    {'label': 'Video to Audio', 'prefix': 'ZYROX-audio'},
    'voice':  {'label': 'Enhance Voice',  'prefix': 'ZYROX-voice-enhanced'},
    '8d':     {'label': '8D Audio',       'prefix': 'ZYROX-8d'},
    'all':    {'label': 'Make All Types', 'prefix': 'ZYROX-mix'},
}


class ProcessError(Exception):
    pass


def clampf(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return float(default)
    return min(max(v, lo), hi)


def run_ff(cmd, timeout=700):
    try:
        p = subprocess.run([FFMPEG] + cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ProcessError('Processing timed out — try a shorter file.')
    except FileNotFoundError:
        raise ProcessError('ffmpeg is not installed on the server.')
    if p.returncode != 0:
        raise ProcessError('Audio engine error: ' + p.stderr.decode('utf-8', 'ignore')[-300:])


def decode_to_wav(inp, out):
    run_ff(['-y', '-i', inp, '-vn', '-ac', '2', '-ar', str(SR), '-t', str(MAX_SECONDS),
            '-sample_fmt', 's16', '-f', 'wav', out])


def wav_duration(wav):
    with wave.open(wav, 'rb') as w:
        return w.getnframes() / w.getframerate()


def encode(inp, out, kbps=192, af=None):
    cmd = ['-y', '-i', inp, '-vn']
    if af:
        cmd += ['-af', af]
    cmd += ['-ac', '2', '-b:a', f'{kbps}k', '-f', 'mp3', out]
    run_ff(cmd)


def make_ir(path, size_s, damp):
    """Generate a stereo impulse response (exponentially decaying noise)."""
    n = int(SR * size_s)
    rng = np.random.default_rng(42)
    L = rng.standard_normal(n).astype(np.float32)
    R = rng.standard_normal(n).astype(np.float32)
    dec = (1.0 - np.arange(n, dtype=np.float32) / n) ** damp
    L *= dec
    R *= dec
    fade = np.ones(n, dtype=np.float32)
    f = int(SR * 0.01)
    if f > 0:
        fade[-f:] = np.linspace(1, 0, f, dtype=np.float32)
    L *= fade
    R *= fade
    m = max(np.abs(L).max(), np.abs(R).max()) or 1.0
    L /= m
    R /= m
    data = np.empty(n * 2, dtype=np.int16)
    data[0::2] = (L * 32767).astype(np.int16)
    data[1::2] = (R * 32767).astype(np.int16)
    with wave.open(path, 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


def apply_8d(wav_in, wav_out, rpm, depth):
    """Rotating 360° pan — streamed block-by-block (low memory)."""
    depth = clampf(depth, 0.2, 3.0, 1.5)
    with wave.open(wav_in, 'rb') as ri:
        ch = ri.getnchannels()
        n = ri.getnframes()
        with wave.open(wav_out, 'wb') as ro:
            ro.setnchannels(2)
            ro.setsampwidth(2)
            ro.setframerate(SR)
            block = 1 << 20
            pos = 0
            while pos < n:
                b = min(block, n - pos)
                raw = ri.readframes(b)
                a = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch).astype(np.float32)
                mono = a.mean(axis=1)
                t = np.arange(pos, pos + b, dtype=np.float32) / SR
                x = np.clip(np.sin(2 * np.pi * (rpm / 60.0) * t) * depth, -1.0, 1.0)
                ang = (x + 1.0) * np.pi / 4.0
                gL = np.cos(ang)
                gR = np.sin(ang)
                dist = 1.0 - 0.16 * (0.5 + 0.5 * np.cos(4 * np.pi * (rpm / 60.0) * t))
                out = np.empty(b * 2, dtype=np.int16)
                out[0::2] = (mono * gL * dist).astype(np.int16)
                out[1::2] = (mono * gR * dist).astype(np.int16)
                ro.writeframes(out.tobytes())
                pos += b


def parse_bands(csv):
    bands = [0.0] * 8
    if csv:
        parts = csv.split(',')
        for i, p in enumerate(parts[:8]):
            bands[i] = clampf(p, -12, 12, 0)
    return bands


def safe_name(inp, tool):
    base = os.path.splitext(os.path.basename(inp))[0][:40] or 'audio'
    safe = ''.join(c for c in base if c.isalnum() or c in ' -_') or 'audio'
    return f"{TOOLS[tool]['prefix']}-{safe}.mp3"


def process(tmpdir, inp, tool, p):
    """Run a tool pipeline. Returns (output_path, download_name, mimetype)."""
    if tool not in TOOLS:
        raise ProcessError('Unknown tool.')
    out = os.path.join(tmpdir, 'out.mp3')
    wav = os.path.join(tmpdir, 'in.wav')
    name = safe_name(inp, tool)

    # ---------- VIDEO → AUDIO (no decode needed) ----------
    if tool == 'v2a':
        run_ff(['-y', '-i', inp, '-vn', '-ac', '2', '-ar', str(SR), '-t', str(MAX_SECONDS),
                '-b:a', '192k', '-f', 'mp3', out])
        return out, name, 'audio/mpeg'

    decode_to_wav(inp, wav)
    dur = wav_duration(wav)

    # ---------- BASS BOOSTED ----------
    if tool == 'bass':
        lvl = clampf(p.get('bass'), 0, 14, 6)
        af = (f'bass=g={lvl}:f=80:w=0.9,'
              f'equalizer=f=125:t=q:w=0.8:g={lvl * 0.6:.1f},' + NF)
        encode(wav, out, af=af)

    # ---------- EQUALIZER ----------
    elif tool == 'eq':
        bands = parse_bands(p.get('bands'))
        parts = [f'equalizer=f={f}:t=q:w=1.05:g={g:.1f}'
                 for f, g in zip(BANDS, bands) if abs(g) >= 0.2]
        af = (','.join(parts) if parts else 'anull') + ',' + NF
        encode(wav, out, af=af)

    # ---------- SLOW REVERB ----------
    elif tool == 'reverb':
        preset = str(p.get('preset') or 'hall').lower()
        sizes = {'room': (0.9, 3.0), 'hall': (2.0, 2.3), 'cathedral': (3.6, 1.7)}
        size, damp = sizes.get(preset, sizes['hall'])
        ir = os.path.join(tmpdir, 'ir.wav')
        make_ir(ir, size, damp)
        wet = clampf(p.get('wet'), 10, 90, 50) / 100.0
        wetg = round(wet * 8.0, 2)
        slow = p.get('slow') == '1'
        src = f'[0:a]asetrate={int(SR * 0.85)},aresample={SR}[dry]' if slow else '[0:a]'
        fc = f'{src};[dry][1:a]afir=dry=10:wet={wetg}[r];[r]{NF}[a]'
        run_ff(['-y', '-i', wav, '-i', ir, '-filter_complex', fc,
                '-map', '[a]', '-b:a', '192k', '-f', 'mp3', out])

    # ---------- CUT AUDIO ----------
    elif tool == 'cut':
        a = clampf(p.get('start_pct'), 0, 99.5, 0) / 100.0 * dur
        b = clampf(p.get('end_pct'), 0.5, 100, 100) / 100.0 * dur
        if b <= a + 0.05:
            b = min(dur, a + 0.05)
        run_ff(['-y', '-ss', f'{a:.3f}', '-to', f'{b:.3f}', '-i', wav,
                '-vn', '-ac', '2', '-b:a', '192k', '-f', 'mp3', out])

    # ---------- ENHANCE VOICE ----------
    elif tool == 'voice':
        cl = clampf(p.get('clarity'), 0, 100, 70) / 100.0
        loud = clampf(p.get('loud'), 0, 8, 4)
        af = ('highpass=f=90,'
              'equalizer=f=240:t=q:w=1:g=-5,'
              f'equalizer=f=3000:t=q:w=1.2:g={4 * cl:.1f},'
              f'equalizer=f=8200:t=q:w=1:g={2 * cl:.1f},'
              f'acompressor=threshold=-24dB:ratio=3.5:attack=6:release=180:makeup={loud},' + NF)
        encode(wav, out, af=af)

    # ---------- 8D AUDIO ----------
    elif tool == '8d':
        rpm = clampf(p.get('speed'), 2, 20, 8)
        depth = clampf(p.get('depth'), 0.3, 3, 1.5)
        w8 = os.path.join(tmpdir, '8d.wav')
        apply_8d(wav, w8, rpm, depth)
        encode(w8, out, af=NF)

    # ---------- MAKE ALL TYPES (presets) ----------
    elif tool == 'all':
        st = str(p.get('song_type') or '8d-bass-boosted')
        w8 = os.path.join(tmpdir, '8d.wav')
        ir = os.path.join(tmpdir, 'ir.wav')
        if st == '8d-bass-boosted':
            apply_8d(wav, w8, 8, 1.5)
            encode(w8, out, af='bass=g=6:f=80:w=0.9,equalizer=f=125:t=q:w=0.8:g=4,' + NF)
        elif st == '3d-mix':
            apply_8d(wav, w8, 4, 0.8)
            make_ir(ir, 2.0, 2.3)
            fc = f'[0:a][1:a]afir=dry=10:wet=2[r];[r]bass=g=4:f=80:w=0.9,{NF}[a]'
            run_ff(['-y', '-i', w8, '-i', ir, '-filter_complex', fc,
                    '-map', '[a]', '-b:a', '192k', '-f', 'mp3', out])
        elif st == '8d-slow-reverb':
            apply_8d(wav, w8, 6, 1.5)
            make_ir(ir, 2.0, 2.3)
            fc = (f'[0:a]asetrate={int(SR * 0.85)},aresample={SR}[dry];'
                  f'[dry][1:a]afir=dry=10:wet=4.5[r];[r]{NF}[a]')
            run_ff(['-y', '-i', w8, '-i', ir, '-filter_complex', fc,
                    '-map', '[a]', '-b:a', '192k', '-f', 'mp3', out])
        elif st == 'ultra-bass-8d':
            apply_8d(wav, w8, 10, 1.8)
            encode(w8, out, af='bass=g=14:f=80:w=0.9,equalizer=f=125:t=q:w=0.8:g=8,' + NF)
        else:
            apply_8d(wav, w8, 8, 1.5)
            encode(w8, out, af='bass=g=6:f=80:w=0.9,' + NF)

    else:
        raise ProcessError('Unknown tool.')

    return out, name, 'audio/mpeg'
