FROM python:3.12-slim
WORKDIR /app
COPY app/ /app/
ENV REGISTRY_FILE=/data/registry.json PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/registry')"
CMD ["python", "server.py"]
