FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=America/New_York

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY nba-fantasy-agent.py roster.csv ./
COPY container/server.py ./container/server.py

RUN mkdir -p reports .cache

EXPOSE 8080
CMD ["python", "container/server.py"]
