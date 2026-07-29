FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8088

COPY src/custom_agent_langchain/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY src/__init__.py /app/src/__init__.py
COPY src/custom_agent_langchain /app/src/custom_agent_langchain
COPY src/custom_agent_langchain/langgraph.json /app/langgraph.json

EXPOSE 8088
CMD ["langgraph", "dev", "--config", "/app/langgraph.json", "--host", "0.0.0.0", "--port", "8088", "--no-browser", "--no-reload"]
