FROM python:3.12-slim

WORKDIR /app

# System deps for pandas/numpy build on ARM
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data dir for SQLite (mount as volume to persist)
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["python", "-u", "main.py"]
