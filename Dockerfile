FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /srv

# Application is stdlib-only: copy source, no package installation and no
# network access required at build time.
COPY app ./app
COPY tests ./tests

# Default service: the long-running review page + API.
EXPOSE 8080
CMD ["python", "-m", "app.main", "--host", "0.0.0.0", "--port", "8080"]
