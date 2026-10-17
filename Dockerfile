FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
# One dependency, approved and pinned: ytmusicapi, for the YouTube playlist. Installed before
# the code is copied so a code change does not reinstall it.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY music2spotify ./music2spotify
COPY tests ./tests
COPY scripts ./scripts
# The Makefile and docs/how.txt are checked against each other by the tests.
COPY Makefile ./
COPY docs ./docs

ENTRYPOINT ["python", "-m", "music2spotify"]
