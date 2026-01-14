FROM denoland/deno:2.1.4

WORKDIR /app/game

# Copy helper library to /app so it's importable as /app/lib.ts
COPY lib.ts /app/lib.ts

# Create data directory for persistence
RUN mkdir -p /data && chown deno:deno /data

# Game directory will be mounted at /app/game (includes server.ts, public/, etc.)
# State will be stored at /data/state.json

USER deno

ENV PORT=8000
ENV STATE_FILE=/data/state.json

EXPOSE 8000

# Default command - game files mounted at /app/game
CMD ["run", "--allow-net=0.0.0.0:8000", "--allow-read=/data,/app", "--allow-write=/data", "--allow-env=PORT,STATE_FILE", "/app/game/server.ts"]
