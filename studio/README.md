# Lumio Studio — инструменты монтажа мультфильмов

Скрипты, на которых собраны «Космонавт», «Соня», «Мама рядом». Правила и промты — в навыке **lumio-cartoon-pipeline**;
здесь — готовые инструменты, чтобы в новом фильме сразу начинать монтаж, а не писать всё заново.

Нужно: Python 3, `opencv-python`, `numpy`, `scipy`, `Pillow`, ffmpeg (с `rubberband` для музыки), шрифт Lora.

## Имена файлов (договорённость)

| Что | Имя |
|---|---|
| Кадры сцен (Seedream) | `f1.jpg … f7.jpg` |
| Сцены (Seedance/Kling) | `s1.mp4 … s7.mp4` |
| Переходы (Kling) | `t1.mp4 …` — переход между `sN` и `sN+1` |
| Музыка (ElevenLabs) | `music.wav` / `music.mp3` |
| Реплики голоса | `vo1.mp3 … vo8.mp3` |

## Шаги

```bash
# 1. Проверить каждый присланный файл
python3 studio/check.py image f3.jpg                 # серая полоса (обрезанный JPEG) -> f3_fixed.jpg
python3 studio/check.py clip  t2.mp4 s2_last.jpg s3_first.jpg   # склейки, водяной знак, совпадение концов

# 2. Черновик (720p-качество, быстро) и финал
python3 studio/build.py film.json draft.mp4 --draft
python3 studio/build.py film.json film_video.mp4
python3 studio/check.py joins film_video.mp4 film_video.mp4.json   # рывки и мерцание на стыках

# 3. Музыка без вырезов: финальный аккорд трека -> на появление логотипа
python3 studio/music_fit.py music.wav music_fit.wav <сек_аккорда_в_треке> <logo_burst> <total>

# 4. Голос + музыка -> готовый фильм
python3 studio/mix.py film_video.mp4 music_fit.wav film.mp4 vo.json

# 5. Версия для Telegram
ffmpeg -i film.mp4 -c:v libx264 -preset slow -crf 21 -maxrate 3M -bufsize 6M -pix_fmt yuv420p \
       -c:a aac -b:a 160k -movflags +faststart film_tg.mp4
```

Пример `film.json` — в начале `build.py`; `logo_burst` и `total` берутся из `film_video.mp4.json`.

## Точечные исправления

- `slowmo.py` — замедлить кусок с дорисовкой кадров (раскачивающийся шаг). Темп по обе стороны стыка — одинаковый.
- `detear.py` — убрать дорожку слезы; рамка не должна задевать подбородок/губы.
- В переходе персонаж в конце должен **стоять** — тогда сцена после него начинается без рывка.

## Пакет промтов

`PROMPT_PACK.md` — шаблон: Claude заполняет его сразу после утверждения сценария (паспорт фильма с чек-листом, все кадры, сцены, переходы, затем музыка, голос и пост) и отдаёт одним файлом.

## Ассеты

- `assets/lumio_outro.mp4` — фирменная концовка (огонёк → логотип из искр → «Мультфильм по вашей истории»), 7.8 с.
- `assets/logo_still.png` — статичный логотип (для обложек, не для концовки).
