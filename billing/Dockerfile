FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    LOG_DIR=/var/log/evcharge BIND_HOST=0.0.0.0 HEALTH_PORT=8002
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin evcharge \
 && mkdir -p /app /var/log/evcharge /app/data && chown -R evcharge /var/log/evcharge /app/data
WORKDIR /app
COPY constraints.txt ./
COPY common/ ./common/
COPY billing/ ./billing/
RUN pip install -c constraints.txt ./common "./billing[pg]"
USER 10001
EXPOSE 8002
HEALTHCHECK --interval=15s --timeout=3s --retries=3 CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/health' % os.environ['HEALTH_PORT'], timeout=2)"
CMD ["python", "-m", "evbilling"]
