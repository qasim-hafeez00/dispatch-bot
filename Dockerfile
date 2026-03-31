# ============================================================
# CortexBot API — Python Dockerfile
# ============================================================

# Use Python 3.12 slim (smaller image, faster builds)
FROM python:3.12-slim

# Set working directory inside container
WORKDIR /app

# Install system dependencies
# These are needed for some Python packages to compile
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (Docker caches this layer)
# If requirements.txt doesn't change, this layer is reused = faster builds
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the entire application
COPY . .

# Create directory for temporary files
RUN mkdir -p /app/tmp

# Expose port 8000 (the FastAPI port)
EXPOSE 8000

# Health check — Docker will restart container if this fails
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Start the FastAPI server
# --host 0.0.0.0: accept connections from any IP (needed in Docker)
# --port 8000: listen on port 8000
# --reload: auto-restart when code changes (development only)
CMD ["uvicorn", "cortexbot.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
