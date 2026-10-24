FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY trader ./trader
COPY config.example.toml .

ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "trader"]
CMD ["run", "--brain", "rules"]
