# TeamBook — блокнот руководителя
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Зависимости ставятся отдельным слоем для кеширования
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY . .

# Данные по умолчанию хранятся в /app/data (переопределяется томом)
ENV HR_DATA_DIR=/app/data

# Не-root пользователь
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app && chmod +x /app/entrypoint.sh
USER appuser

EXPOSE 5000
ENTRYPOINT ["/app/entrypoint.sh"]