FROM debian:bookworm-slim

ARG PAYMENT_SANDBOX_VERSION=1.5.4
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) expected="704b1c5ae85f4b4548125c334631e471cb6b0f5e0b92fb7212dae4cf263086f2" ;; \
      arm64) expected="d25f71d7575ac1ba102c9b79dd771daf0329ac323be1ec7f5dfa1d9aa64c5f7e" ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    archive="payment-sandbox_${PAYMENT_SANDBOX_VERSION}_linux_${TARGETARCH}.tar.gz"; \
    url="https://github.com/proxynth/payment-sandbox/releases/download/v${PAYMENT_SANDBOX_VERSION}/${archive}"; \
    curl --fail --location --proto '=https' --tlsv1.2 "$url" -o "/tmp/$archive"; \
    echo "$expected  /tmp/$archive" | sha256sum --check -; \
    tar -xzf "/tmp/$archive" -C /tmp; \
    binary="$(find /tmp -type f -name payment-sandbox -perm -111 -print -quit)"; \
    test -n "$binary"; \
    install -m 0755 "$binary" /usr/local/bin/payment-sandbox; \
    rm -rf /tmp/*

RUN useradd --system --uid 10001 --create-home payment-sandbox \
    && mkdir -p /var/lib/payment-sandbox \
    && chown payment-sandbox:payment-sandbox /var/lib/payment-sandbox

USER payment-sandbox
WORKDIR /var/lib/payment-sandbox

ENV PAYMENT_SANDBOX_HTTP_ADDRESS=:8080
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/payment-sandbox"]
