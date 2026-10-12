FROM mcr.microsoft.com/playwright/python:v1.63.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY VERSION .
COPY entrypoint.sh .

ENV DATA_DIR=/data HOME=/home/pwuser
VOLUME /data
EXPOSE 8000

# Starts as root only to make /data writable, then runs the app as pwuser
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
