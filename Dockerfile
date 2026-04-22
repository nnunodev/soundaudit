FROM python:3.12-alpine

RUN apk add --no-cache \
    ffmpeg \
    gcc \
    musl-dev \
    libffi-dev

WORKDIR /workspace
COPY pyproject.toml .
COPY src ./src

RUN pip install --no-cache-dir -e ".[dev]"

COPY . .
ENTRYPOINT ["python", "-m", "soundaudit.cli"]
