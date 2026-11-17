# Backend image: used by node-a, node-b, node-c, the one-shot migrate job and the bench client.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt
COPY backend backend
COPY load_testing load_testing
COPY scripts scripts
RUN useradd --create-home tatkalx && chown -R tatkalx /srv
USER tatkalx
WORKDIR /srv/backend
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=2s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "5"]
