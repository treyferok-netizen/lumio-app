# Пакет промтов: «<НАЗВАНИЕ>»

Заполняется Claude сразу после утверждения сценария и отдаётся Роме одним файлом.
Генерировать по порядку: каждый следующий кадр строится от готового предыдущего.
Имена файлов: кадры `f1.jpg…`, сцены `s1.mp4…`, переходы `t1.mp4…`, голос `vo1.mp3…`, музыка `music.wav`.

---

## 0. Паспорт фильма (одна таблица — проверяем ДО генераций)

| Сцена | Возраст героев | Место / время года / время суток | Действие (одно!) | Эмоция | Чем заканчивается (поза для перехода) |
|---|---|---|---|---|---|
| s1 | | | | | |
| s2 | | | | | |
| … | | | | | |

- Внешность героев (повторяется в КАЖДОМ промте кадра): `<цвет волос>, <цвет глаз>, <причёска>, <одежда-якорь>`
- Сквозной мотив: маленький золотой огонёк (light orb).
- Реалии: <российские / другие>.
- Длительность: ~<N> с без концовки; концовка — `studio/assets/lumio_outro.mp4`.

**Чек-лист паспорта** (если хоть один пункт «нет» — правим сценарий, не кредиты):
- [ ] в каждой сцене одно понятное действие;
- [ ] у каждого возраста есть внешние признаки (рост, лицо, одежда);
- [ ] сцена, после которой идёт переход, заканчивается спокойной позой (персонаж стоит/обнимает, не идёт);
- [ ] нет надписей в кадре (плакаты, ленты, вывески — «без текста»);
- [ ] слёзы — только «блеск в глазах», стартовые кадры без слёз.

---

## 1. Кадры (Seedream)

### f1 — <сцена 1>
image 1: —
```
<описание кадра>. <внешность героев>. A tiny golden glowing light orb near <где> (no insects). Pixar-style 3D animation, 16:9, realistic proportions (not chibi), no text, no letters, no labels, no signs.
```

### f2 — <сцена 2>
image 1: последний кадр `s1.mp4` (Claude вырежет и пришлёт)
```
Same <герои> as in image 1 — keep their faces, <внешность героев>. <что изменилось: возраст, место>. <описание кадра>. A tiny golden glowing light orb near <где> (no insects). Match the art style of image 1. Pixar-style 3D animation, 16:9, realistic proportions (not chibi), no text, no letters, no labels, no signs.
```
<!-- повторить для каждой сцены; для взрослых героев — image 2 = кадр, где лица крупно -->

---

## 2. Сцены (Seedance 2.5 · 16:9 · 1080P · 7 с · звук ВЫКЛ)

### s1
image 1: `f1.jpg`
```
<место, свет>. <действие — одно, по шагам>. <эмоция без слёз: "glossy eyes, calm happy smile, she is not crying">. In the last second, the golden light orb glows a little brighter. Gentle slow camera, natural calm movements, correct hands with five fingers. Only quiet room sounds, no music. Pixar-style 3D animation.
```
<!-- для каждой сцены -->

---

## 3. Переходы (Kling · First + Last frame [+ Middle] · 5 с, с тремя кадрами 10 с)

Сначала ОДИН переход как проба → Claude показывает его в контексте → остальные.

### t1: s1 → s2
First: `t1_A.jpg` (последний кадр s1) · Last: `t1_B.jpg` (первый кадр s2)
```
Magical time-passage transition. The tiny golden light orb near <герои> slowly grows into a large soft glowing golden sphere of light with sparkles. <герои> keep calm gentle expressions and stay still until the light covers them; no tears. Inside the glowing sphere <место А> softly dissolves and morphs into <место Б>, <деталь времени: листья / звёзды / лепестки>. At the end <персонаж> stands still, he does not walk yet. Only one moment is visible at a time, no duplicate people. Calm slow dissolve, smooth continuous camera, no hard cuts, no zoom burst, no motion blur. Pixar-style 3D animation.
```
<!-- для каждого перехода -->

---

## 4. Музыка (ElevenLabs Music · без вокала · длительность = фильм + 10 с)

Заполняется после утверждения картинки, по таймкодам из `film_video.mp4.json`.
```
<жанр, инструменты, тональность, ~68 BPM>, no vocals.
0:00–0:<..> — <настроение сцены 1–2>
…
<..>–<..> — emotional peak (<какая сцена>)
<logo_burst> — warm final chord, then gentle shimmering fade out.
```

---

## 5. Голос (ElevenLabs v3 · голос: <название сохранённого голоса>)

Тег по умолчанию: `[soft but clear voice, speaking close and intimate, not whispering]`

| № | Сцена | Начало (с) | Реплика |
|---|---|---|---|
| vo1 | s1 | | |
| … | | | |

---

## 6. Пост

- Обложка: <кадр>
- Текст: <черновик>
- Кнопки:
```
🎬 <Заказать …> - https://t.me/LumioMultBot/app
💬 Задать вопрос - https://t.me/LumioMultBot
```
