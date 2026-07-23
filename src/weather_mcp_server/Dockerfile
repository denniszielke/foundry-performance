FROM python:3.13-slim
WORKDIR /app
COPY ./src/weather_mcp_server/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY ./src /app/src
ENV WEATHER_MCP_HOST=0.0.0.0
ENV WEATHER_MCP_PORT=8093
EXPOSE 8093
CMD ["python", "-m", "src.weather_mcp_server.server"]
