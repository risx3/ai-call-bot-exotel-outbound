FROM dailyco/pipecat-base:latest

# Set working directory
WORKDIR /app

# Environment variables for uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Install uv explicitly
RUN pip install --no-cache-dir uv

# Install dependencies using lockfile
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=/app/uv.lock \
    --mount=type=bind,source=pyproject.toml,target=/app/pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Copy application files
COPY bot.py ./bot.py
COPY server.py ./server.py
COPY prompts.py ./prompts.py

# Expose FastAPI / WS port
EXPOSE 7860

# Start server
CMD ["uv", "run", "server.py"]
