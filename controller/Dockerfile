FROM python:3.12-alpine

WORKDIR /app
COPY controller/controller.py .

STOPSIGNAL SIGTERM
ENTRYPOINT ["python3", "/app/controller.py"]
