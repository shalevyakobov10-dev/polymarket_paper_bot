FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 DATA_DIR=/app/data
WORKDIR /app
COPY app.py paper_trading.py /app/
RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()" || exit 1
CMD ["python", "app.py"]
