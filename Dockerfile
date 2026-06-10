# Build frontend
FROM node:20-alpine AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --legacy-peer-deps
COPY frontend/ ./
RUN npm run build

# Build backend
FROM python:3.12-slim
WORKDIR /app

# Install git and curl since some VCS tasks and API calls require them
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy python project requirements
COPY pyproject.toml uv.lock README.md ./
# Install pip dependencies (including our web panel optional dependencies)
RUN pip install --no-cache-dir .[gigachat,openrouter,web]

# Copy harness_bench package and web panel package
COPY harness_bench/ ./harness_bench/
COPY web/ ./web/

# Copy built frontend static files
COPY --from=frontend-builder /app/frontend/out/ ./frontend/out/

EXPOSE 8765

ENV PYTHONUNBUFFERED=1
ENV PORT=8765

CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8765"]
