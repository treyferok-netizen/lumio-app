"""Подогнать трек ElevenLabs под фильм БЕЗ вырезов: одно равномерное изменение темпа.

    python3 studio/music_fit.py track.wav out.wav chord_in_track film_logo_time film_total

chord_in_track — секунда финального аккорда в исходном треке (найди на слух или по пику громкости),
film_logo_time — когда в фильме огонёк рассыпается в логотип (logo_burst из build.py .json),
film_total — длина фильма. Темп меняется на chord/logo (обычно 1–2%, высота не меняется),
хвост мягко затухает к последнему кадру.
"""
import subprocess
import sys

src, out, chord, logo, total = sys.argv[1], sys.argv[2], *map(float, sys.argv[3:6])
tempo = chord / logo
fade = min(1.8, total * 0.05)
print(f"темп x{tempo:.4f} ({(tempo - 1) * 100:+.1f}%)")
if abs(tempo - 1) > 0.04:
    print("ВНИМАНИЕ: больше 4% — может быть слышно; лучше сгенерировать трек нужной длины")
subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-af",
                f"rubberband=tempo={tempo}:pitchq=quality:transients=smooth,atrim=0:{total},"
                f"afade=t=out:st={total - fade}:d={fade}:curve=qsin", "-ar", "48000", out], check=True)
print("готово:", out)
