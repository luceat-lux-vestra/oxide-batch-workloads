FROM golang:1.27.3-bookworm AS campaign-control-build

ARG PAYMENT_SANDBOX_SOURCE_REPOSITORY=https://github.com/proxynth/payment-sandbox.git
ARG PAYMENT_SANDBOX_SOURCE_REVISION=3b6d6e1864ea5a6d017fcb005f547cf26c37352a

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN git init /src \
    && cd /src \
    && git remote add origin "$PAYMENT_SANDBOX_SOURCE_REPOSITORY" \
    && git fetch --depth=1 origin "$PAYMENT_SANDBOX_SOURCE_REVISION" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$PAYMENT_SANDBOX_SOURCE_REVISION"

COPY campaign-control/main.go /src/cmd/payment-sandbox-campaign-control/main.go

RUN cd /src \
    && CGO_ENABLED=0 go build -trimpath -o /out/payment-sandbox-campaign-control \
        ./cmd/payment-sandbox-campaign-control


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

COPY --from=campaign-control-build /out/payment-sandbox-campaign-control /usr/local/bin/payment-sandbox-campaign-control

RUN useradd --system --uid 10001 --create-home payment-sandbox \
    && mkdir -p /var/lib/payment-sandbox \
    && chown payment-sandbox:payment-sandbox /var/lib/payment-sandbox

USER payment-sandbox
WORKDIR /var/lib/payment-sandbox

ENV PAYMENT_SANDBOX_HTTP_ADDRESS=:8080
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/payment-sandbox"]
