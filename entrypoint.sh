#!/bin/sh
# Если пароль не задан через HR_PASSWORD — генерируем и выводим для пользователя.
if [ -z "$HR_PASSWORD" ]; then
  HR_PASSWORD=$(head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 14)
  echo "============================================================"
  echo "[TeamBook] Переменная HR_PASSWORD не задана."
  echo "[TeamBook] Сгенерирован пароль для входа: $HR_PASSWORD"
  echo "[TeamBook] Сохраните и установите HR_PASSWORD, иначе пароль"
  echo "[TeamBook] сменится после пересоздания контейнера."
  echo "============================================================"
  export HR_PASSWORD
fi

exec gunicorn --bind 0.0.0.0:${PORT:-5000} --workers ${WEB_CONCURRENCY:-2} wsgi:app