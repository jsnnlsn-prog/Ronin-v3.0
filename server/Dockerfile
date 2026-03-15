FROM python:3.12-slim

LABEL maintainer="Dopamine Ronin"
LABEL description="RONIN MCP Tool Server"

# System deps for shell execution tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    jq \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server code
COPY . .

# Create RONIN directories
RUN mkdir -p /root/.ronin/workspace

EXPOSE 8741

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8741')" || exit 1

ENTRYPOINT ["python", "ronin_mcp_server.py"]
CMD ["--transport", "http", "--port", "8741"]
