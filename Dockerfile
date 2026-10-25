FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt requirements-browser.txt ./
RUN pip install --no-cache-dir -r requirements.txt
# Optional: build with --build-arg INSTALL_BROWSER=1 to support JavaScript-rendered sites (adds ~400 MB)
ARG INSTALL_BROWSER=0
RUN if [ "$INSTALL_BROWSER" = "1" ]; then pip install --no-cache-dir -r requirements-browser.txt && playwright install --with-deps chromium; fi
COPY . .
EXPOSE 8000
# There is no login. Only expose this port to people you trust (keep it on localhost or behind a VPN/firewall).
CMD ["python", "-m", "src.web", "--host", "0.0.0.0", "--port", "8000"]
