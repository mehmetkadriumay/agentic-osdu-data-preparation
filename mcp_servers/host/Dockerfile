FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends libstdc++6 \
    && find /var/lib/apt/lists -type f -delete
COPY pyproject.toml README.md ./
COPY src ./src
COPY mcp_servers ./mcp_servers
RUN pip install --no-cache-dir . -r mcp_servers/host/requirements.txt \
    && useradd --uid 10001 --create-home osdu
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "mcp_servers.host.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
