FROM node:22-slim
RUN apt-get update && apt-get install -y python3 python3-venv curl && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir \
    claude-agent-sdk opentelemetry-sdk opentelemetry-exporter-otlp-proto-http aiohttp
COPY runner.py /runner.py
ENV PATH=/venv/bin:$PATH
CMD ["python3","/runner.py"]
