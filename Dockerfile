FROM python:3.12-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY frontend ./frontend
COPY eval ./eval

# The sample DB is seeded deterministically on first startup; mount a
# volume only if you want it to persist across containers.
RUN mkdir -p data
VOLUME /srv/data

EXPOSE 8001
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
