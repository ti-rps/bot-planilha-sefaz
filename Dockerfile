# bot-planilha-sefaz — worker headless (FASE 6).
#
# Roda worker.py consumindo a fila do rps-maestro. Chrome/ChromeDriver vêm do
# apt do Debian (chromium + chromium-driver são version-matched pelo mantenedor,
# sem repo externo nem mismatch de versão). O código já lê CHROME_BINARY,
# CHROMEDRIVER_PATH e HEADLESS por env (FASE 3) — só setamos os defaults aqui.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HEADLESS=true \
    CHROME_BINARY=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver

# chromium-driver: ChromeDriver casado com a versão do chromium.
# fonts-liberation: o portal renderiza CAPTCHA/imagens — sem fontes, layout quebra.
# tini: PID 1 que repassa SIGTERM ao worker (graceful shutdown do signal_handler).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-liberation \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala deps antes do código para aproveitar cache de layer.
COPY requirements-worker.txt .
RUN pip install -r requirements-worker.txt

COPY . .

# Usuário não-root (sem privilégio — o share entra por bind mount do host).
RUN useradd --create-home --uid 10001 worker \
    && mkdir -p /app/downloads /app/log \
    && chown -R worker:worker /app
USER worker

# tini como PID 1 → SIGTERM chega no Python → _signal_handler fecha a conexão
# RabbitMQ e sai limpo (sem deixar job órfão em running no Maestro).
ENTRYPOINT ["tini", "--"]
CMD ["python", "worker.py"]
