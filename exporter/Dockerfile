FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY modem_exporter ./modem_exporter

RUN useradd --system --uid 10001 exporter
USER exporter

EXPOSE 9877
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('LISTEN_PORT','9877'), timeout=3)"

ENTRYPOINT ["python", "-m", "modem_exporter"]
CMD ["serve"]
