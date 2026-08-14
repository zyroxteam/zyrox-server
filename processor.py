"""ZYROX MUSIC ENHANCE — server-side audio engine (ffmpeg + numpy)."""
import os
import re
import subprocess
import wave

import numpy as np

SR = 44100
MAX_SECONDS = 900          # hard cap: 15 minutes
TARGET_MEAN = -14.0        # loudness target (dB)
PEAK_CEIL = -0.5           # safety ceiling (dB)

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


class Cancelled(ProcessError):
    pass


def clampf(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return float(default)
    return min(max(v, lo), hi)


_ffmpeg_cache = [None]


def get_ffmpeg():
    """Resolve the ffmpeg binary: env override → bundled static build → system ffmpeg."""
    if _ffmpeg_cache[0] is None:
        cand = os.environ.get('FFMPEG_PATH')
        if not cand:
            try:
                import imageio_ffmpeg
                cand = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                cand = None
        _ffmpeg_cache[0] = cand or 'ffmpeg'
    return _ffmpeg_cache[0]


def media_duration(path):
    """Fast duration probe (header read only)."""
    try:
        p = subprocess.run([get_ffmpeg(), '-i', path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           text=True, timeout=30)
        m = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.?\d*)', p.stderr or '')
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return None


def run_ff(cmd, timeout=700, dur=None, prog=None, cancel=None):
    """Run ffmpeg. `prog(0..1)` gets live progress when `dur` is known;
    `cancel()` (returns bool) aborts processing."""
    full = [get_ffmpeg(), '-y', '-v', 'error']
    if prog and dur:
        full += ['-progress', 'pipe:1', '-nostats']
    full += cmd
    try:
        p = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    except FileNotFoundError:
        raise ProcessError('ffmpeg is not installed on the server.')

    if prog and dur:
        try:
            for line in p.stdout:
                if line.startswith('out_time_ms='):
                    try:
                        us = int(line.strip().split('=')[1])
                        frac = min(1.0, max(0.0, us / (dur * 1e6)))
                        prog(frac)
                    except ValueError:
                        pass
                if cancel and cancel():
                    p.terminate()
                    p.wait(timeout=10)
                    raise Cancelled('Cancelled by user.')
        except Cancelled:
            raise
        except Exception:
            pass

    try:
        _, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        raise ProcessError('Processing timed out — try a shorter file.')

    if p.returncode != 0:
        raise ProcessError('Audio engine error: ' + (err or '')[-300:])
    if cancel and cancel():
        raise Cancelled('Cancelled by user.')
    if prog:
        prog(1.0)


def decode_to_wav(inp, out, prog=None, cancel=None):
    dur = media_duration(inp)
    d = min(dur or 0, MAX_SECONDS)

    def cb(frac):
        if prog:
            prog(frac * 0.10)
    run_ff(['-i', inp, '-vn', '-ac', '2', '-ar', str(SR), '-t', str(MAX_SECONDS),
            '-sample_fmt', 's16', '-f', 'wav', out],
           dur=d if d else None, prog=cb if d else None, cancel=cancel)


def wav_duration(wav):
    with wave.open(wav, 'rb') as w:
        return w.getnframes() / w.getframerate()


def volume_gain(wav):
    """Fast loudness scan → static gain (replaces slow loudnorm filter)."""
    p = subprocess.run([get_ffmpeg(), '-i', wav, '-af', 'volumedetect',
                        '-f', 'null', '/dev/null'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       text=True, timeout=120)
    txt = p.stderr or ''
    m_mean = re.search(r'mean_volume:\s*(-?[\d.]+)\s*dB', txt)
    m_max = re.search(r'max_volume:\s*(-?[\d.]+)\s*dB', txt)
    mean = float(m_mean.group(1)) if m_mean else TARGET_MEAN
    peak = float(m_max.group(1)) if m_max else -1.0
    gain = TARGET_MEAN - mean
    gain = min(gain, PEAK_CEIL - peak)   # never clip
    gain = max(-20.0, min(20.0, gain))
    return round(gain, 2)


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


def apply_8d(wav_in, wav_out, rpm, depth, prog=None, cancel=None):
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
                if cancel and cancel():
                    raise Cancelled('Cancelled by user.')
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
                if prog:
                    prog(0.10 + 0.35 * (pos / n))


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


def process(tmpdir, inp, tool, p, prog=None, cancel=None):
    """Run a tool pipeline. Returns (output_path, download_name, mimetype)."""
    if tool not in TOOLS:
        raise ProcessError('Unknown tool.')
    out = os.path.join(tmpdir, 'out.mp3')
    wav = os.path.join(tmpdir, 'in.wav')
    name = safe_name(inp, tool)

    def enc_with_gain(src_wav, af_prefix, start=0.45, end=1.0):
        """Static loudness normalization + encode with live progress."""
        gain = volume_gain(src_wav)
        af = (af_prefix + ',' if af_prefix else '') + \
             f'volume={gain}dB,alimiter=limit=0.95'
        dur = wav_duration(src_wav)

        def cb(frac):
            if prog:
                prog(start + (end - start) * frac)
        run_ff(['-i', src_wav, '-vn', '-af', af, '-ac', '2',
                '-b:a', '192k', '-f', 'mp3', out],
               dur=dur, prog=cb, cancel=cancel)

    def cb_done():
        if prog:
            prog(1.0)

    # ---------- VIDEO → AUDIO ----------
    if tool == 'v2a':
        dur = media_duration(inp)
        run_ff(['-i', inp, '-vn', '-ac', '2', '-ar', str(SR), '-t', str(MAX_SECONDS),
                '-b:a', '192k', '-f', 'mp3', out],
               dur=min(dur or 0, MAX_SECONDS) if dur else None,
               prog=prog if dur else None, cancel=cancel)
        cb_done()
        return out, name, 'audio/mpeg'

    decode_to_wav(inp, wav, prog=prog, cancel=cancel)
    dur = wav_duration(wav)

    # ---------- BASS BOOSTED ----------
    if tool == 'bass':
        lvl = clampf(p.get('bass'), 0, 14, 6)
        af = f'bass=g={lvl}:f=80:w=0.9,equalizer=f=125:t=q:w=0.8:g={lvl * 0.6:.1f}'
        enc_with_gain(wav, af)

    # ---------- EQUALIZER ----------
    elif tool == 'eq':
        bands = parse_bands(p.get('bands'))
        parts = [f'equalizer=f={f}:t=q:w=1.05:g={g:.1f}'
                 for f, g in zip(BANDS, bands) if abs(g) >= 0.2]
        enc_with_gain(wav, ','.join(parts) if parts else None)

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
        gain = volume_gain(wav)
        src = f'[0:a]asetrate={int(SR * 0.85)},aresample={SR}[dry]' if slow else '[0:a]'
        fc = (f'{src};[dry][1:a]afir=dry=10:wet={wetg}[r];'
              f'[r]volume={gain}dB,alimiter=limit=0.95[a]')

        def cb(frac):
            if prog:
                prog(0.10 + 0.90 * frac)
        run_ff(['-i', wav, '-i', ir, '-filter_complex', fc,
                '-map', '[a]', '-b:a', '192k', '-f', 'mp3', out],
               dur=dur, prog=cb, cancel=cancel)
        cb_done()

    # ---------- CUT AUDIO ----------
    elif tool == 'cut':
        a = clampf(p.get('start_pct'), 0, 99.5, 0) / 100.0 * dur
        b = clampf(p.get('end_pct'), 0.5, 100, 100) / 100.0 * dur
        if b <= a + 0.05:
            b = min(dur, a + 0.05)
        cut_wav = os.path.join(tmpdir, 'cut.wav')
        run_ff(['-ss', f'{a:.3f}', '-to', f'{b:.3f}', '-i', wav,
                '-vn', '-ac', '2', '-sample_fmt', 's16', '-f', 'wav', cut_wav],
               cancel=cancel)
        enc_with_gain(cut_wav, None)

    # ---------- ENHANCE VOICE ----------
    elif tool == 'voice':
        cl = clampf(p.get('clarity'), 0, 100, 70) / 100.0
        loud = clampf(p.get('loud'), 0, 8, 4)
        af = ('highpass=f=90,'
              'equalizer=f=240:t=q:w=1:g=-5,'
              f'equalizer=f=3000:t=q:w=1.2:g={4 * cl:.1f},'
              f'equalizer=f=8200:t=q:w=1:g={2 * cl:.1f},'
              f'acompressor=threshold=-24dB:ratio=3.5:attack=6:release=180:makeup={loud}')
        enc_with_gain(wav, af)

    # ---------- 8D AUDIO ----------
    elif tool == '8d':
        rpm = clampf(p.get('speed'), 2, 20, 8)
        depth = clampf(p.get('depth'), 0.3, 3, 1.5)
        w8 = os.path.join(tmpdir, '8d.wav')
        apply_8d(wav, w8, rpm, depth, prog=prog, cancel=cancel)
        enc_with_gain(w8, None, start=0.45, end=1.0)

    # ---------- MAKE ALL TYPES (presets) ----------
    elif tool == 'all':
        st = str(p.get('song_type') or '8d-bass-boosted')
        w8 = os.path.join(tmpdir, '8d.wav')
        ir = os.path.join(tmpdir, 'ir.wav')
        if st == '8d-bass-boosted':
            apply_8d(wav, w8, 8, 1.5, prog=prog, cancel=cancel)
            enc_with_gain(w8, 'bass=g=6:f=80:w=0.9,equalizer=f=125:t=q:w=0.8:g=4',
                          start=0.45, end=1.0)
        elif st == '3d-mix':
            apply_8d(wav, w8, 4, 0.8, prog=prog, cancel=cancel)
            make_ir(ir, 2.0, 2.3)
            gain = volume_gain(w8)
            fc = (f'[0:a][1:a]afir=dry=10:wet=2[r];'
                  f'[r]bass=g=4:f=80:w=0.9,volume={gain}dB,alimiter=limit=0.95[a]')

            def cb3(frac):
                if prog:
                    prog(0.45 + 0.55 * frac)
            run_ff(['-i', w8, '-i', ir, '-filter_complex', fc,
                    '-map', '[a]', '-b:a', '192k', '-f', 'mp3', out],
                   dur=wav_duration(w8), prog=cb3, cancel=cancel)
            cb_done()
        elif st == '8d-slow-reverb':
            apply_8d(wav, w8, 6, 1.5, prog=prog, cancel=cancel)
            make_ir(ir, 2.0, 2.3)
            gain = volume_gain(w8)
            fc = (f'[0:a]asetrate={int(SR * 0.85)},aresample={SR}[dry];'
                  f'[dry][1:a]afir=dry=10:wet=4.5[r];'
                  f'[r]volume={gain}dB,alimiter=limit=0.95[a]')

            def cb4(frac):
                if prog:
                    prog(0.45 + 0.55 * frac)
            run_ff(['-i', w8, '-i', ir, '-filter_complex', fc,
                    '-map', '[a]', '-b:a', '192k', '-f', 'mp3', out],
                   dur=wav_duration(w8), prog=cb4, cancel=cancel)
            cb_done()
        elif st == 'ultra-bass-8d':
            apply_8d(wav, w8, 10, 1.8, prog=prog, cancel=cancel)
            enc_with_gain(w8, 'bass=g=14:f=80:w=0.9,equalizer=f=125:t=q:w=0.8:g=8',
                          start=0.45, end=1.0)
        else:
            apply_8d(wav, w8, 8, 1.5, prog=prog, cancel=cancel)
            enc_with_gain(w8, 'bass=g=6:f=80:w=0.9', start=0.45, end=1.0)

    else:
        raise ProcessError('Unknown tool.')

    cb_done()
    return out, name, 'audio/mpeg'
