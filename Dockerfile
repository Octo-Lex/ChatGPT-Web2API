FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget gnupg2 curl \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google.list \
    && apt-get update && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

# Persistent Chrome profile (stores login session)
VOLUME /data/chrome-profile

# Cookie file mount point (for headless auth)
VOLUME /data/cookies

ENV W2A_HEADLESS=true
ENV W2A_USER_DATA_DIR=/data/chrome-profile
ENV W2A_PORT=8080

EXPOSE 8080 9222

# Start script handles cookie injection
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

ENTRYPOINT ["/docker-entrypoint.sh"]
