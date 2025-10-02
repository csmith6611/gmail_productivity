FROM python:3.11-slim

WORKDIR /app

# Install gcc and other build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    libffi-dev \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY email_watcher.py .


CMD ["python", "email_watcher.py"]