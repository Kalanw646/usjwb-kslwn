FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx curl unzip procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Xray core (V2Ray) — latest stable release for linux/amd64
RUN curl -L -o /tmp/xray.zip https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip \
    && unzip /tmp/xray.zip -d /usr/local/bin/ \
    && rm /tmp/xray.zip \
    && chmod +x /usr/local/bin/xray \
    && /usr/local/bin/xray version
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY . .
COPY nginx.conf.template /etc/nginx/nginx.conf.template
RUN chmod +x start.sh
EXPOSE 8080
CMD ["./start.sh"]
