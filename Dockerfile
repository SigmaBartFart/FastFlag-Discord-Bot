FROM python:3.12.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.lock ./requirements.lock
RUN python -m pip install --no-cache-dir --requirement requirements.lock

COPY bot.py env.example README.md ./

RUN mkdir -p /app/data && chmod 755 /app/data

CMD ["python", "bot.py"]
