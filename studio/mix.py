"""Свести голос и музыку и вложить в видео. Музыка под голосом стабильно уходит вниз (фиксированная огибающая).

    python3 studio/mix.py video.mp4 music.wav out.mp4 vo.json [duck_db]

vo.json: [["vo1.mp3", 0.8], ["vo2.mp3", 11.4], ...]   — файл реплики и секунда начала.
Каждая реплика: обрезка тишины -> highpass -> компрессор -> -18 LUFS; лёгкое «комнатное» эхо.
Музыка: -12 dB (по умолчанию) за 0.7 с до реплики и до 0.7 с после, плавно.
В конце печатает «голос над музыкой» по каждой реплике (норма ~+15 dB, минимум +4 dB в любом месте речи).
"""
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
from scipy.io import wavfile

VID, MUS, OUT, VOJ = sys.argv[1:5]
DUCK = float(sys.argv[5]) if len(sys.argv) > 5 else -12.0
SR = 48000
T = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", VID],
                         capture_output=True, text=True).stdout)
N = int(T * SR)
tmp = tempfile.mkdtemp()


def load(path, ch):
    p = os.path.join(tmp, f"x{np.random.randint(1e9)}.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-ar", str(SR), "-ac", str(ch), p], check=True)
    _, a = wavfile.read(p)
    return a.astype(np.float32) / 32768


mus = load(MUS, 2)
mus = np.pad(mus, ((0, max(0, N - len(mus))), (0, 0)))[:N]
vox = np.zeros(N, np.float32)
env = np.zeros(N, np.float32)
lines = []
for f, t in json.load(open(VOJ)):
    p = os.path.join(tmp, "vo.wav")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", f, "-af",
                    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05,areverse,"
                    "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15,areverse,"
                    "highpass=f=80,acompressor=threshold=-26dB:ratio=3:attack=5:release=180:makeup=2,"
                    "loudnorm=I=-18:TP=-2:LRA=5", "-ar", str(SR), "-ac", "1", p], check=True)
    _, v = wavfile.read(p)
    v = v.astype(np.float32) / 32768
    i = int(t * SR)
    v = v[:N - i]
    vox[i:i + len(v)] += v
    env[max(0, i - int(0.7 * SR)):min(N, i + len(v) + int(0.7 * SR))] = 1
    lines.append((f, i, len(v)))
k = int(0.35 * SR)
ker = np.hanning(2 * k + 1)
env = np.clip(np.convolve(env, ker / ker.sum(), mode="same"), 0, 1)
md = mus * (10 ** (DUCK * env / 20))[:, None]
wavfile.write(os.path.join(tmp, "m.wav"), SR, (np.clip(md, -1, 1) * 32767).astype(np.int16))
wavfile.write(os.path.join(tmp, "v.wav"), SR, (np.clip(vox, -1, 1) * 32767).astype(np.int16))
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", VID, "-i", os.path.join(tmp, "m.wav"), "-i", os.path.join(tmp, "v.wav"),
                "-filter_complex", "[2:a]aformat=channel_layouts=stereo,aecho=0.8:0.4:45|90:0.10|0.05,volume=1.25[v];"
                "[1:a]volume=0.8[m];[m][v]amix=inputs=2:normalize=0,loudnorm=I=-15:TP=-1.5:LRA=11,aresample=48000[a]",
                "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", OUT],
               check=True)
for f, i, n in lines:
    rv = np.sqrt((vox[i:i + n] ** 2).mean()) * 1.25
    rm = np.sqrt((md[i:i + n] ** 2).mean()) * 0.8 + 1e-9
    print(f"{os.path.basename(f)}: голос над музыкой {20 * np.log10(rv / rm):+.1f} dB")
