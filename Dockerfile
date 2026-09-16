FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /uvx /bin/

# Ambiente fora de /app de propósito: o docker-compose faz bind-mount da
# pasta inteira (.:/app) pra live-reload em dev -- um venv criado dentro
# de /app no build seria escondido pelo bind-mount do host em runtime.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

COPY . .

CMD ["python", "explore/inspect_garmin.py"]
