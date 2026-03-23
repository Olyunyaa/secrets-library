FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_salebot.py .
COPY knowledge_base.js .
COPY roadmap_all_pains_v4.json .

# Data directory for persistent storage (mounted as volume)
RUN mkdir -p /data

CMD ["python", "bot_salebot.py"]
