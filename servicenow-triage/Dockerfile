# Prediction API. Models and logs live on mounted volumes, not in the image.
#   docker build -t servicenow-triage .
#   docker run -p 8080:8080 -e TRIAGE_API_KEY=... \
#     -v $PWD/models:/app/models:ro -v $PWD/logs:/app/logs servicenow-triage
FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system triage && mkdir -p models logs && chown triage logs
COPY config.yaml ./
ENV TRIAGE_CONFIG=/app/config.yaml
USER triage
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
# One worker is plenty (a prediction takes milliseconds) and keeps /admin/reload simple.
CMD ["uvicorn", "triage.serve:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
