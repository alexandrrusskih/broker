FROM rust:1.90.0-trixie AS rust
FROM broker-box

COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo \
    RUSTUP_HOME=/usr/local/rustup \
    PATH=/usr/local/cargo/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential pkg-config libssl-dev \
 && rm -rf /var/lib/apt/lists/* \
 && rustup component add rustfmt clippy \
 && cargo --version && rustc --version && cargo fmt --version \
 && cargo clippy --version && cc --version | head -1
