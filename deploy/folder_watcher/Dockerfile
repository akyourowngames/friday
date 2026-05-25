FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 7474
CMD ["python", "folder_watcher_service.py", "run", "--config", "tools/FOLDER_WATCHER_CONFIG.md", "--host", "0.0.0.0"]
