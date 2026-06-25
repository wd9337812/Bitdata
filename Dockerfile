FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY README.md .

ENV APP_HOST=0.0.0.0
ENV APP_PORT=8080
ENV APP_CONFIG_PATH=/app/data/config.json
EXPOSE 8080

CMD ["python", "-m", "app.main"]
