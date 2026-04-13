FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer, only rebuilt when requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Run as non-root user for security
RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 6002

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "6002"]
