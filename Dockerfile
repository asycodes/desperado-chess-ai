FROM python:3.12-slim

# Stockfish (used from part 3): Debian installs it at /usr/games/stockfish
RUN apt-get update && apt-get install -y --no-install-recommends stockfish \
    && rm -rf /var/lib/apt/lists/*
ENV STOCKFISH_PATH=/usr/games/stockfish

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ENV PORT=8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
