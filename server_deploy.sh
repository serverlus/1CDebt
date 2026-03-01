# ============================================================
# Деплой License Server на VPS (Ubuntu 22.04)
# ============================================================
# Минимальные требования: 1 vCPU, 512MB RAM, Ubuntu 22.04
# Стоимость: ~300-500 ₽/мес (Timeweb, REG.RU, Selectel)
# ============================================================

# ── ШАГ 1: Подготовка сервера ────────────────────────────────

# Обновляем систему
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv nginx certbot python3-certbot-nginx

# Создаём пользователя для сервиса
sudo useradd -m -s /bin/bash debtscoring
sudo su - debtscoring

# Копируем файлы (с твоего компа):
#   scp -r ./license_server debtscoring@ВАШ_IP:~/

# ── ШАГ 2: Установка зависимостей ────────────────────────────

cd ~/license_server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# ── ШАГ 3: Переменные окружения ──────────────────────────────

# Создаём .env файл (ЗАМЕНИ ЗНАЧЕНИЯ!)
cat > ~/.env << 'EOF'
DS_ADMIN_TOKEN=сгенерируй_токен_python3_-c_import_secrets_print_secrets.token_hex_32
DS_HMAC_SECRET=сгенерируй_другой_токен_python3_-c_import_secrets_print_secrets.token_hex_32
DS_DATABASE_URL=sqlite+aiosqlite:///./licenses.db
DS_DB_FILE=./licenses.db
DS_DEBUG=false
DS_GRACE_HOURS=72
EOF

# Проверяем что сервер стартует
source venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8000 &
curl http://127.0.0.1:8000/api/v1/health
# Ожидаем: {"status":"ok","service":"DebtScoring License Server",...}
kill %1

# ── ШАГ 4: Systemd сервис ─────────────────────────────────────

sudo tee /etc/systemd/system/debtscoring.service << 'EOF'
[Unit]
Description=DebtScoring License Server
After=network.target

[Service]
Type=simple
User=debtscoring
WorkingDirectory=/home/debtscoring/license_server
EnvironmentFile=/home/debtscoring/.env
ExecStart=/home/debtscoring/license_server/venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5

# Безопасность
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable debtscoring
sudo systemctl start debtscoring
sudo systemctl status debtscoring  # Должно быть: active (running)

# ── ШАГ 5: Nginx + HTTPS ─────────────────────────────────────

# Замени license.твойдомен.ru на свой домен!
sudo tee /etc/nginx/sites-available/debtscoring << 'EOF'
server {
    listen 80;
    server_name license.твойдомен.ru;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 30;
    }
}
EOF

sudo ln -s /etc/nginx/sites-available/debtscoring /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx

# Получаем SSL сертификат (бесплатный Let's Encrypt)
sudo certbot --nginx -d license.твойдомен.ru --non-interactive --agree-tos -m ты@email.ru

# ── ШАГ 6: Проверка ──────────────────────────────────────────

curl https://license.твойдомен.ru/api/v1/health
# Ожидаем: {"status":"ok",...}

# ── ШАГ 7: Создаём первые ключи для продажи ──────────────────

curl -X POST https://license.твойдомен.ru/api/v1/admin/generate \
  -H "Authorization: Bearer ВАШ_DS_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "plan": "PRO",
    "count": 5,
    "months": 12,
    "comment": "Первая партия для продажи",
    "invoice": "СЧ-001"
  }'

# Пример ответа:
# {
#   "keys": [
#     "DSCR-A1B2-C3D4-E5F6-G7H8",
#     "DSCR-B2C3-D4E5-F6G7-H8I9",
#     ...
#   ],
#   "plan": "PRO",
#   "months": 12
# }

# ── Полезные команды ─────────────────────────────────────────

# Логи сервиса
sudo journalctl -u debtscoring -f

# Статистика лицензий
curl -H "Authorization: Bearer ВАШ_DS_ADMIN_TOKEN" \
     https://license.твойдомен.ru/api/v1/admin/stats

# Отозвать лицензию
curl -X POST https://license.твойдомен.ru/api/v1/admin/revoke \
  -H "Authorization: Bearer ВАШ_DS_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"license_key": "DSCR-XXXX-XXXX-XXXX-XXXX", "reason": "Возврат средств"}'

# Бэкап базы данных (добавь в cron)
# 0 3 * * * cp ~/license_server/licenses.db ~/backups/licenses_$(date +%Y%m%d).db
