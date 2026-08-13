# VergeX Render Proxy

Minimal Render deployment for CLIProxyAPI v7.2.125 plus the VergeX Responses compatibility filter.

Required Render secrets:

- `CLIPROXY_CONFIG_B64`: base64-encoded CLIProxyAPI YAML configuration.
- `CLIPROXY_AUTH_B64`: base64-encoded Codex OAuth JSON record.

The public API remains protected by the API key declared inside the configuration. No credential is committed to this repository.
