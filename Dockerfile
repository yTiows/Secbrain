FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY brain ./brain
COPY web ./web
RUN pip install --no-cache-dir ".[phone]"
ENV BRAIN_DATA_DIR=/data BRAIN_HOST=0.0.0.0
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/api/v1/health')" || exit 1
CMD ["brain", "serve", "--port", "8765"]
