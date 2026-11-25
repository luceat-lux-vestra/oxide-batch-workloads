FROM rust:1.99.1-bookworm AS build

WORKDIR /src
COPY payment-reference ./payment-reference

RUN cargo build \
    --release \
    --manifest-path payment-reference/Cargo.toml \
    --package payment-reference-gateway

FROM debian:bookworm-slim

# Reqwest's platform verifier needs the system CA store, including for HTTP-only clients.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && test -s /etc/ssl/certs/ca-certificates.crt

RUN useradd --system --uid 10002 --create-home gateway

COPY --from=build /src/payment-reference/target/release/payment-reference-gateway /usr/local/bin/payment-reference-gateway

USER gateway
WORKDIR /home/gateway

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/payment-reference-gateway"]
