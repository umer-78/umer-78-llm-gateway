FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml config.yaml ./
COPY gateway ./gateway
COPY bench ./bench
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["uvicorn", "gateway.server:app", "--host", "0.0.0.0", "--port", "8000"]
