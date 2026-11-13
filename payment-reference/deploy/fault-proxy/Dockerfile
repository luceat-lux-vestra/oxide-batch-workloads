FROM python:3.13.18-slim-bookworm

WORKDIR /app
COPY payment-reference/deploy/fault-proxy/proxy.py /app/proxy.py

RUN useradd --system --uid 10003 --create-home proxy

USER proxy

ENV PROXY_LISTEN_ADDRESS=0.0.0.0
ENV PROXY_LISTEN_PORT=8080
ENV UPSTREAM_HOST=payment-sandbox
ENV UPSTREAM_PORT=8080

EXPOSE 8080

ENTRYPOINT ["python3", "/app/proxy.py"]
