FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/agents/backend:/app/agents
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && find /var/lib/apt/lists -type f -delete
COPY pyproject.toml README.md ./
COPY src ./src
COPY agents/backend ./agents/backend
COPY agents/osdu_foundry ./agents/osdu_foundry
RUN pip install --no-cache-dir . -r agents/backend/requirements.txt \
    && useradd --uid 10001 --create-home osdu
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "osdu_cloud.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
