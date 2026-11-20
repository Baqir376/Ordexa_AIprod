FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations
RUN pip install --no-cache-dir . && useradd --create-home ordexa
USER ordexa
EXPOSE 8000
CMD ["uvicorn", "ordexa.apps.api:app", "--host", "0.0.0.0", "--port", "8000"]
