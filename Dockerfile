# Foothold server — the FastAPI backend that the Capacitor app loads.
# Only the backend ships in the image; the Android client is built separately.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY _env_bootstrap.py /app/
COPY targets/ /app/targets/
COPY templates/ /app/templates/

ENV FOOTHOLD_DB=postgres \
    FOOTHOLD_RESEARCH_MODE=queue \
    FOOTHOLD_PORT=8300 \
    PYTHONUNBUFFERED=1

EXPOSE 8300

CMD ["python", "targets/server.py"]
