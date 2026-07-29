FROM python:3.13-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8088

COPY src/custom_agent_maf/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY src/__init__.py /app/src/__init__.py
COPY src/custom_agent_maf /app/src/custom_agent_maf

EXPOSE 8088
CMD ["python", "-m", "src.custom_agent_maf.agent"]
