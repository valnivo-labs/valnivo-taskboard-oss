FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 HOST=0.0.0.0 PORT=8790 \
    TASKBOARD_DB=/data/taskboard.db TASKBOARD_BACKUP_DIR=/data/backups
RUN useradd --system --uid 10001 --create-home taskboard && mkdir /data && chown taskboard /data
WORKDIR /app
COPY server.py mcp_server.py ./
COPY static ./static
COPY docs ./docs
USER taskboard
VOLUME /data
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python3 -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/api/health'%os.environ.get('PORT','8790'),timeout=3)"
CMD ["python3", "server.py"]
