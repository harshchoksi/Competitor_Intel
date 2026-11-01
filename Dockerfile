FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SEMANTIC_EMBEDDINGS=off
COPY requirements.txt requirements-browser.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Optional: build with --build-arg INSTALL_BROWSER=1 to support JavaScript-rendered sites (adds ~400 MB; needs >= 1 GB RAM)
ARG INSTALL_BROWSER=0
RUN if [ "$INSTALL_BROWSER" = "1" ]; then pip install --no-cache-dir -r requirements-browser.txt && playwright install --with-deps chromium; fi
COPY . .
RUN mkdir -p /app/data
EXPOSE 8000
# ONE process only: analyses run as threads inside it and their live state is in its memory. More than one worker/process
# would split that state (jobs would look lost). PORT is injected by Render/Railway/most hosts.
CMD python -m src.web --host 0.0.0.0 --port ${PORT:-8000}
