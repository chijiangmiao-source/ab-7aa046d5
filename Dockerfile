FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    HOST=0.0.0.0 \
    DATA_FILE=/data/verdicts.json

WORKDIR /srv

COPY app/ ./app/
COPY tests/ ./tests/
COPY verify/ ./verify/

# Build check: every source file must compile.
RUN python3 -m compileall -q app tests verify

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=5 \
  CMD python3 -c "import json,urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3); sys.exit(0 if json.load(r).get('status')=='ok' else 1)"

CMD ["python3", "app/server.py"]
