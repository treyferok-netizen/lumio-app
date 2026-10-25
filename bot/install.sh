#!/usr/bin/env bash
# Lumio bot installer for Ubuntu 22.04/24.04. Run as root:
#   curl -fsSL https://raw.githubusercontent.com/treyferok-netizen/lumio-app/main/bot/install.sh | bash
set -euo pipefail
REPO="https://github.com/treyferok-netizen/lumio-app.git"
DIR=/opt/lumio-app
ENVF=/etc/lumio-bot.env
IP=$(curl -4 -fsS https://api.ipify.org || hostname -I | awk '{print $1}')
HOST="${IP//./-}.sslip.io"

say(){ printf '\n\033[1;33m%s\033[0m\n' "$*"; }
ask(){ local v; read -r -p "$1" v </dev/tty; echo "$v"; }
asks(){ local v; read -r -s -p "$1" v </dev/tty; echo >&2; echo "$v"; }

say "1/5  Пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3-venv caddy curl >/dev/null

say "2/5  Код бота"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q; else git clone -q --depth 1 "$REPO" "$DIR"; fi
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q -r "$DIR/bot/requirements.txt"

say "3/5  Настройки (вводимые токены не отображаются на экране)"
if [ -f "$ENVF" ]; then
  echo "Файл настроек уже есть: $ENVF — оставляю его. Изменить: lumio-set ИМЯ ЗНАЧЕНИЕ"
else
  BOT_TOKEN=$(asks "Токен @LumioMultBot из @BotFather: ")
  API_KEY=$(asks "API-ключ YandexGPT (Yandex Cloud): ")
  FOLDER=$(ask "ID каталога Yandex Cloud [b1gl7ldfqd6mglo4onti]: "); FOLDER=${FOLDER:-b1gl7ldfqd6mglo4onti}
  ADMIN=$(ask "Ваш chat id (если не знаете — просто Enter, добавим позже): ")
  umask 077
  cat > "$ENVF" <<EOF
BOT_TOKEN=$BOT_TOKEN
YANDEX_API_KEY=$API_KEY
FOLDER_ID=$FOLDER
ADMIN_CHAT_ID=$ADMIN
WEBHOOK_SECRET=$(openssl rand -hex 16)
EOF
fi

say "4/5  HTTPS для заявок из Mini App"
busy(){ ss -ltnH "( sport = :$1 )" | grep -q .; }
if busy 80 && ! ss -ltnpH "( sport = :80 )" | grep -q caddy; then
  echo "Порт 80 занят другой программой — HTTPS-сертификат не получить. Напишите Claude, что выдал этот шаг:"; ss -ltnp "( sport = :80 )"; exit 1
fi
if busy 443 && ! ss -ltnpH "( sport = :443 )" | grep -q caddy; then
  PORT=8443; ADDR="$HOST:8443"; GLOBAL="{
	https_port 8443
}"
else
  PORT=443; ADDR="$HOST"; GLOBAL=""
fi
cat > /etc/caddy/Caddyfile <<EOF
$GLOBAL
$ADDR {
	reverse_proxy 127.0.0.1:8080
}
EOF
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow 80/tcp >/dev/null; ufw allow "$PORT"/tcp >/dev/null; fi
systemctl enable -q caddy; systemctl restart caddy

say "5/5  Запуск бота"
cat > /etc/systemd/system/lumio-bot.service <<EOF
[Unit]
Description=Lumio Telegram bot
After=network-online.target
[Service]
EnvironmentFile=$ENVF
WorkingDirectory=$DIR/bot
ExecStart=$DIR/.venv/bin/python -u runner.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
cat > /usr/local/bin/lumio-set <<'EOF'
#!/usr/bin/env bash
# lumio-set ADMIN_CHAT_ID 123456  — изменить настройку и перезапустить бота
set -e; F=/etc/lumio-bot.env; K="$1"; V="$2"
if grep -q "^$K=" "$F"; then sed -i "s|^$K=.*|$K=$V|" "$F"; else echo "$K=$V" >> "$F"; fi
systemctl restart lumio-bot && echo "OK: $K обновлён, бот перезапущен"
EOF
cat > /usr/local/bin/lumio-update <<EOF
#!/usr/bin/env bash
# lumio-update — забрать новую версию бота из GitHub и перезапустить
set -e; git -C $DIR pull -q && $DIR/.venv/bin/pip install -q -r $DIR/bot/requirements.txt && systemctl restart lumio-bot && echo "OK: бот обновлён"
EOF
chmod +x /usr/local/bin/lumio-set /usr/local/bin/lumio-update
systemctl daemon-reload; systemctl enable -q lumio-bot; systemctl restart lumio-bot
sleep 4
journalctl -u lumio-bot -n 5 --no-pager -o cat || true

URL="https://$ADDR/order"
say "Готово!"
echo "Адрес для заявок (пришлите его Claude):  $URL"
echo "Проверка:  curl -s https://$ADDR/"
echo "Логи бота: journalctl -u lumio-bot -f"
