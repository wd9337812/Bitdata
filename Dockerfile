FROM node:22-slim AS frontend

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend ./
RUN npm run build

FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY --from=frontend /app/static ./app/static
COPY README.md .

ENV APP_HOST=0.0.0.0
ENV APP_PORT=8080
ENV APP_CONFIG_PATH=/app/data/config.json
EXPOSE 8080

CMD ["python", "-m", "app.main"]
