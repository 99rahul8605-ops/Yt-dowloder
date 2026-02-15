FROM python:3.11-slim

# Install ffmpeg (required for audio conversion)
RUN apt update && apt install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Expect cookies.txt to be mounted at /app/cookies.txt
# BOT_TOKEN must be provided as environment variable
CMD ["python", "bot.py"]
