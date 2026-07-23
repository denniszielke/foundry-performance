FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8088 \
    WEATHER_TOOL_MODE=toolbox

COPY src/hosted_agent/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY src/__init__.py /app/src/__init__.py
COPY src/agent_common /app/src/agent_common
COPY src/hosted_agent /app/src/hosted_agent

EXPOSE 8088
CMD ["python", "-m", "src.hosted_agent.agent"]
