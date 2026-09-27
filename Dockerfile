FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server ./server
COPY web ./web
ENV SHC_DATA_DIR=/data PORT=8000
VOLUME /data
EXPOSE 8000
# One worker keeps the SQLite file simple; threads handle many phones + the controller.
CMD ["sh", "-c", "gunicorn -w 1 --threads 8 -b 0.0.0.0:${PORT} 'server.app:create_app()'"]
