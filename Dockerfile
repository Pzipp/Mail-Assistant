FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY assistant.py api.py scheduler.sh entrypoint.sh ./
COPY prompts/ prompts/
RUN chmod +x scheduler.sh entrypoint.sh

VOLUME ["/data"]

CMD ["/app/entrypoint.sh"]
