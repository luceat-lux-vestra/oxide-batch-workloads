FROM python:3.13.18-slim-bookworm

WORKDIR /app
COPY payment-reference/deploy/fault-proxy/proxy.py /app/proxy.py
COPY payment-reference/deploy/fault-proxy/request_fault_adapter.py /app/request_fault_adapter.py
COPY payment-reference/deploy/fault-proxy/webhook_scheduler.py /app/webhook_scheduler.py

USER 65534:65534

ENV PROXY_LISTEN_ADDRESS=0.0.0.0
ENV PROXY_LISTEN_PORT=8080
ENV UPSTREAM_HOST=payment-sandbox
ENV UPSTREAM_PORT=8080

EXPOSE 8080

ENTRYPOINT ["python3", "/app/proxy.py"]
