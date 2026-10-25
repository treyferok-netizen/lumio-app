# Бот Lumio: заявки из Mini App и ответы клиентам

Одна функция Yandex Cloud делает всё:
- принимает заявки из Mini App (с фото) и присылает их администратору в @LumioMultBot;
- отвечает клиентам на вопросы через YandexGPT по файлу `knowledge.md`;
- если не уверен — пересылает вопрос администратору; ответ администратора (реплаем) уходит клиенту.

## Что поменять без программиста
- **Ответы бота** — правьте `knowledge.md` и загрузите функцию заново (новая версия).
- **Цены** — в `knowledge.md` и в `index.html` Mini App (блок `CONFIG`).

## Переменные окружения функции
| Имя | Что это |
| --- | --- |
| `BOT_TOKEN` | токен @LumioMultBot из @BotFather |
| `WEBHOOK_SECRET` | любая длинная случайная строка (латиница и цифры) |
| `PUBLIC_URL` | ссылка на функцию, `https://functions.yandexcloud.net/…` |
| `ADMIN_CHAT_ID` | ваш chat id (бот покажет его по команде `/myid`) |
| `FOLDER_ID` | ID каталога Yandex Cloud |

## Служебные ссылки
- Подключить вебхук: `PUBLIC_URL?src=setup&key=WEBHOOK_SECRET` — открыть один раз в браузере.
- Mini App шлёт заявки на `PUBLIC_URL?src=order`.
