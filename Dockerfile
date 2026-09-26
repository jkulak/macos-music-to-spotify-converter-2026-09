FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
# Standard library only: nothing to pip install.
COPY music2spotify ./music2spotify
COPY tests ./tests

ENTRYPOINT ["python", "-m", "music2spotify"]
