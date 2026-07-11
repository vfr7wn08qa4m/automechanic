# Бекэнд для Oracle Free Tier ARM VM (или любого docker-хоста)
#   docker build -t automech-api -f backend/Dockerfile .
#   docker run -d -p 8000:8000 --env-file .env -v ./data:/app/data automech-api
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-backend.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-backend.txt
COPY pipeline ./pipeline
COPY backend ./backend
EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
