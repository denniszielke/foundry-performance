FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8088

COPY src/hosted_agent_responses/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY src/__init__.py /app/src/__init__.py
COPY src/hosted_agent_responses /app/src/hosted_agent_responses

EXPOSE 8088
CMD ["python", "-m", "src.hosted_agent_responses.agent"]
