# Finders: one container serving the API and the built web app.
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
COPY scripts ./scripts
COPY demo ./demo
COPY --from=web /web/dist ./web/dist
ENV HOST=0.0.0.0 PORT=8078 FINDERS_DB=/data/finders.db
VOLUME /data
EXPOSE 8078
CMD ["uv", "run", "--no-dev", "python", "-c", "from finders.api import main; main()"]
