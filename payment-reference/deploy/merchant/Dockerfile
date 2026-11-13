FROM rust:1.99.1-bookworm AS build

WORKDIR /src
COPY payment-reference ./payment-reference

RUN cargo build \
    --release \
    --manifest-path payment-reference/Cargo.toml \
    --package payment-reference-merchant

FROM debian:bookworm-slim

RUN useradd --system --uid 10004 --create-home merchant

COPY --from=build /src/payment-reference/target/release/payment-reference-merchant /usr/local/bin/payment-reference-merchant

USER merchant
WORKDIR /home/merchant

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/payment-reference-merchant"]
