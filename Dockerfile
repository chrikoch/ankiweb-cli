FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

ENV HOME=/tmp
USER 65532:65532
ENTRYPOINT ["ankiweb-cli"]
