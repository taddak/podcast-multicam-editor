# Isolated way to run the agent for a week: the container sees only its own code, the run folder
# volume and the keys passed at runtime (never baked into the image).
FROM python:3.13-slim

RUN useradd --create-home --uid 10001 trader
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --disable-pip-version-check --require-hashes --only-binary=:all: -r requirements.lock
COPY trader ./trader
COPY config.example.toml .
RUN mkdir -p /app/runs && chown trader:trader /app/runs

USER trader
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "-m", "trader"]
CMD ["run", "--brain", "rules"]
