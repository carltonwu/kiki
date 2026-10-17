# Built from the repo root (shared Cargo workspace):
#   docker build -f services/ingester/Dockerfile .
FROM rust:1.91-slim-bookworm AS chef
RUN cargo install cargo-chef --locked --version ^0.1
WORKDIR /app

FROM chef AS planner
COPY . .
RUN cargo chef prepare --recipe-path recipe.json --bin ingester

# Dependencies build in their own layer, so code-only changes rebuild quickly.
FROM chef AS builder
COPY --from=planner /app/recipe.json recipe.json
RUN cargo chef cook --release --recipe-path recipe.json --bin ingester
COPY . .
RUN cargo build --release --bin ingester

FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 kiki
COPY --from=builder /app/target/release/ingester /usr/local/bin/ingester
USER kiki
ENV LOG_FORMAT=json
CMD ["ingester"]
