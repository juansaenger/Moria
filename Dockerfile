FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install . && useradd --create-home --uid 1000 homebot

USER homebot
ENV HOMEBOT_WORKSPACE=/app/workspace
CMD ["python", "-m", "homebot"]
