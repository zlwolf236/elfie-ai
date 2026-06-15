FROM python:3.12-slim

WORKDIR /app

# libgomp1 is required by Silero VAD (PyTorch)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY elfie_agent.py .
COPY elfie/ ./elfie/

# Pre-download Silero VAD weights so the first session isn't slow
RUN python -c "from livekit.plugins import silero; silero.VAD.load()" 2>/dev/null || true

CMD python elfie_agent.py start \
    --url "$LIVEKIT_URL" \
    --api-key "$LIVEKIT_API_KEY" \
    --api-secret "$LIVEKIT_API_SECRET"
