"""Монтаж на сервере Lumio (ffmpeg): склейка кусков видео с переходами, позже — голос и музыка.

Спецификация (JSON):
{
  "name": "ogonek_t8_test",                 # имя результата (латиница)
  "segments": [
    {"src": "video-af0213a9.mp4", "start": 0, "end": 3.0},
    {"src": "video-1d54edfa.mp4", "start": 4.3, "transition": "fadeblack", "dur": 0.6}
  ],
  "audio": [                                # необязательно
    {"src": "vo1.mp3", "at": 2.0, "volume": 1.0},
    {"src": "music.mp3", "at": 0, "volume": 0.25, "fade_out": 3}
  ]
}
src — имя файла в /files, ссылка на наш /files или внешняя ссылка.
Картинка как сегмент: {"src": "bg.jpg", "image": true, "length": 3.2}; поверх любого сегмента можно
положить PNG с прозрачностью: "overlay": "layer.png" (растягивается на 1920x1080, кладётся ПОСЛЕ crop).
"crop": [x, y, w, h] — приблизить часть кадра (координаты 1920x1080).
transition — переход ПЕРЕД сегментом: cut (по умолчанию), fade, fadeblack, fadewhite, dissolve; dur — секунды.
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

FPS = 24
W, H = 1920, 1080
_lock = threading.Lock()   # один монтаж за раз (1 vCPU)


def ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def duration(path):
    exe = ffmpeg_exe()
    p = subprocess.run([exe, "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", p.stderr)
    if not m:
        raise RuntimeError("не удалось узнать длительность: " + os.path.basename(path))
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def build(spec, resolve, out_path, log=print):
    """resolve(src) -> локальный путь. Возвращает длительность результата."""
    exe = ffmpeg_exe()
    if not exe:
        raise RuntimeError("на сервере нет ffmpeg (lumio-update поставит imageio-ffmpeg)")
    segs = spec.get("segments") or []
    if not segs:
        raise RuntimeError("пустой список segments")
    args, filters, lens = [exe, "-y", "-hide_banner", "-loglevel", "error"], [], []
    seg_in, overlays = [], []      # индексы входов: сначала все сегменты, потом картинки-наложения
    for i, s in enumerate(segs):
        path = resolve(s["src"])
        if s.get("image"):        # картинка как кусок видео: {"src": "x.jpg", "image": true, "dur": 3}
            d = float(s.get("dur_image") or s.get("length") or 3.0)
            args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", path]
            lens.append(d)
        else:
            start = float(s.get("start") or 0)
            end = s.get("end")
            end = float(end) if end is not None else duration(path)
            if end <= start:
                raise RuntimeError(f"сегмент {i}: end <= start")
            args += ["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", path]
            lens.append(end - start)
        seg_in.append(i)
    n_seg = len(segs)
    for i, s in enumerate(segs):   # "overlay": PNG с прозрачностью поверх сегмента (растягивается на 1920x1080)
        if s.get("overlay"):
            args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{lens[i]:.3f}", "-i", resolve(s["overlay"])]
            overlays.append((i, n_seg + len(overlays)))
    ov_of = dict(overlays)
    for i, s in enumerate(segs):
        base = (f"[{i}:v]fps={FPS},scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        if s.get("crop"):          # "crop": [x, y, w, h] в координатах 1920x1080 — приблизить часть кадра
            cx, cy, cw, ch = [int(v) for v in s["crop"]]
            base += f",crop={cw}:{ch}:{cx}:{cy},scale={W}:{H}:flags=lanczos,setsar=1"
        if i in ov_of:
            filters.append(base + f"[b{i}]")
            filters.append(f"[{ov_of[i]}:v]scale={W}:{H},format=rgba[o{i}]")
            filters.append(f"[b{i}][o{i}]overlay=0:0:format=auto,format=yuv420p,settb=AVTB[v{i}]")
        else:
            filters.append(base + f",format=yuv420p,settb=AVTB[v{i}]")
    cur, total = "v0", lens[0]
    for i in range(1, len(segs)):
        tr = (segs[i].get("transition") or "cut").lower()
        d = float(segs[i].get("dur") or 0.5)
        out = f"x{i}"
        if tr == "cut" or d <= 0:
            filters.append(f"[{cur}][v{i}]concat=n=2:v=1:a=0[{out}]")
            total += lens[i]
        else:
            d = min(d, lens[i] - 0.05, total - 0.05)
            name = {"fade": "fade", "dissolve": "dissolve", "fadeblack": "fadeblack",
                    "fadewhite": "fadewhite"}.get(tr, "fade")
            filters.append(f"[{cur}][v{i}]xfade=transition={name}:duration={d:.3f}:offset={total - d:.3f}[{out}]")
            total += lens[i] - d
        cur = out
    # аудио
    auds = spec.get("audio") or []
    amix = []
    n_in = len(segs) + len(overlays)
    for j, a in enumerate(auds):
        # start/dur — взять кусок исходного трека (секунды внутри файла)
        if a.get("start"):
            args += ["-ss", f"{float(a['start']):.3f}"]
        if a.get("dur"):
            args += ["-t", f"{float(a['dur']):.3f}"]
        args += ["-i", resolve(a["src"])]
        k = n_in + j
        at = int(float(a.get("at") or 0) * 1000)
        vol = float(a.get("volume", 1.0))
        f = f"[{k}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={vol}"
        if a.get("fade_in"):
            f += f",afade=t=in:st=0:d={float(a['fade_in'])}"
        if a.get("fade_out") and a.get("dur"):    # затухание в конце куска
            fo = float(a["fade_out"])
            f += f",afade=t=out:st={max(0.0, float(a['dur']) - fo):.3f}:d={fo}"
        f += f",adelay={at}|{at}"
        if a.get("fade_out") and not a.get("dur"):
            fo = float(a["fade_out"])
            f += f",afade=t=out:st={max(0.0, total - fo):.3f}:d={fo}"
        f += f"[a{j}]"
        filters.append(f)
        amix.append(f"[a{j}]")
    duck = [f"[a{j}]" for j, a in enumerate(auds) if a.get("duck")]
    main = [f"[a{j}]" for j, a in enumerate(auds) if not a.get("duck")]
    if duck and main:   # музыка приглушается под голосом (sidechain)
        filters.append("".join(main) + f"amix=inputs={len(main)}:normalize=0:duration=longest,asplit=2[vo][sc]")
        filters.append("".join(duck) + f"amix=inputs={len(duck)}:normalize=0:duration=longest[mus]")
        # sc дополняем тишиной: иначе компрессор обрывает музыку, когда кончается последняя реплика
        filters.append("[sc]apad[scp]")
        filters.append("[mus][scp]sidechaincompress=threshold=0.02:ratio=6:attack=40:release=600:makeup=1[musd]")
        filters.append(f"[vo][musd]amix=inputs=2:normalize=0:duration=longest,alimiter=limit=0.95,atrim=0:{total:.3f}[aout]")
    elif amix:
        filters.append("".join(amix) + f"amix=inputs={len(amix)}:normalize=0:duration=longest,"
                       f"atrim=0:{total:.3f}[aout]")
    args += ["-filter_complex", ";".join(filters), "-map", f"[{cur}]"]
    # кодирование: по умолчанию быстрое рабочее; для Telegram — "encode": {"preset":"slow","crf":21,"maxrate":"3M","audio":"160k"}
    enc = spec.get("encode") or {}
    if amix:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", str(enc.get("audio", "192k"))]
    else:
        args += ["-an"]
    args += ["-c:v", "libx264", "-preset", str(enc.get("preset", "veryfast")), "-crf", str(enc.get("crf", 19))]
    if enc.get("maxrate"):
        mr = str(enc["maxrate"])
        args += ["-maxrate", mr, "-bufsize", str(enc.get("bufsize") or (str(int(float(mr.rstrip("Mk")) * 2)) + mr[-1]))]
    args += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", "-t", f"{total:.3f}", out_path]
    log("ffmpeg: " + " ".join(a if len(a) < 200 else a[:200] + "…" for a in args))
    p = subprocess.run(args, capture_output=True, text=True, timeout=3600)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + (p.stderr or "")[-800:])
    return total
