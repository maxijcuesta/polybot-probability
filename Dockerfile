FROM python:3.12-slim

# Evitar archivos .pyc y buffering de stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencias de sistema mínimas (gcc para compilar eth-account en ARM)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Dependencias Python — capa separada para cache de Docker
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código fuente
COPY . .

# Directorio para logs locales (la DB va en el volumen /data)
RUN mkdir -p logs

# Nota: corre como root para que el volumen /data (montado por Fly.io) sea
# escribible en el primer arranque. Aceptable para un bot de investigación.

# Health check: el dashboard expone /health en 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python scripts/healthcheck.py || exit 1

EXPOSE 8080

CMD ["bash", "scripts/start_cloud.sh"]
