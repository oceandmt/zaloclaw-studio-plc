# zaloclaw-studio — production image (Option B).
# Contains both runtimes: Python (webapp) + Node (bridge/sessiond/zca-js).
FROM node:20-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Source (heavy dirs excluded via .dockerignore: .venv, node_modules, data, logs, .git)
COPY . .

# Normalise perms (a restrictive local umask can leave files 0600 and break the
# non-root/default container user; git only records the exec bit, not read bits).
RUN chmod -R a+rX /app

# Python deps in a venv (mirrors the systemd install layout)
RUN python3 -m venv webapp/.venv \
 && webapp/.venv/bin/pip install --no-cache-dir --upgrade pip \
 && webapp/.venv/bin/pip install --no-cache-dir -r requirements.txt

# Node deps for the Zalo bridge
RUN cd bridge && (npm ci --omit=dev || npm install --omit=dev)

RUN mkdir -p data logs

ENV PYTHONUNBUFFERED=1 \
    ZS_ROLE=all \
    PATH="/app/webapp/.venv/bin:$PATH"

WORKDIR /app/webapp
EXPOSE 18090 18091
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "18090", "--log-level", "warning"]
