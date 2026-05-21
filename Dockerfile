FROM python:3.12-alpine

WORKDIR /app

RUN pip install requests websocket-client

COPY unique_agent.py /app/unique_agent.py

CMD ["python", "-u", "/app/unique_agent.py"]
