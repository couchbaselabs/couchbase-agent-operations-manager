# Optional corporate CA certificates

If you build behind a TLS-inspecting proxy (Zscaler, Netskope, etc.), put your
proxy's root CA here as a `.crt` file (PEM) before running `docker build`,
or run `scripts/setup-corporate-ca.sh`. Any `*.crt` in this folder is added to
the image trust store at build time. `*.crt` files here are gitignored.
