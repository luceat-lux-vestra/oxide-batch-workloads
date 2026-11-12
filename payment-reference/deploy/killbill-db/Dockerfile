FROM postgres:18.6-bookworm

ARG KILLBILL_SOURCE_COMMIT=4f3fa45dbb044ed5d65cccebb0beadbc5d1b2899

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY payment-reference/deploy/killbill-db/killbill-ddl.lock /tmp/killbill-ddl.lock
COPY payment-reference/adapters/killbill-payment-sandbox/src/main/resources/ddl-postgresql.sql /tmp/payment-sandbox-plugin-ddl.sql

RUN set -eux; \
    output=/docker-entrypoint-initdb.d/010-killbill.sql; \
    printf '\\set ON_ERROR_STOP on\n\n' > "$output"; \
    while read -r expected path; do \
      case "$expected" in ''|'#'*) continue ;; esac; \
      tmp="/tmp/$(basename "$path")"; \
      url="https://raw.githubusercontent.com/killbill/killbill/${KILLBILL_SOURCE_COMMIT}/${path}"; \
      curl --fail --location --proto '=https' --tlsv1.2 "$url" -o "$tmp"; \
      size="$(wc -c < "$tmp" | tr -d ' ')"; \
      actual="$( (printf 'blob %s\0' "$size"; cat "$tmp") | sha1sum | awk '{print $1}' )"; \
      test "$actual" = "$expected"; \
      printf '\n-- source: %s @ %s\n' "$path" "$expected" >> "$output"; \
      cat "$tmp" >> "$output"; \
      printf '\n' >> "$output"; \
      rm -f "$tmp"; \
    done < /tmp/killbill-ddl.lock; \
    printf '\n-- repository-owned payment-sandbox plugin DDL\n' >> "$output"; \
    cat /tmp/payment-sandbox-plugin-ddl.sql >> "$output"; \
    rm -f /tmp/payment-sandbox-plugin-ddl.sql /tmp/killbill-ddl.lock

HEALTHCHECK --interval=5s --timeout=3s --start-period=10s --retries=20 \
  CMD pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" || exit 1
