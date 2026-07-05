# Repository Guidelines

## Project Structure & Module Organization
- `vpngate.py` fetches VPN Gate CSV data, selects the best server, and writes `vpngate.ovpn`.
- `docker-compose.yml` defines the GlueTun VPN, Transmission, and miniDLNA services.
- `docker-compose.override.yml` is for local overrides (UID/GID, mounts, friendly name).
- `Dockerfile` builds a minimal Transmission daemon image.
- `downloads/` is the default local mount for completed media.
- `transmission-settings.json` stores Transmission configuration defaults.

## Build, Test, and Development Commands
- `python3 vpngate.py` generates/refreshes `vpngate.ovpn` from VPN Gate.
- `docker-compose up -d` starts GlueTun, Transmission, and miniDLNA in the background.
- `docker-compose down` stops and removes the running containers.
- `docker build -t private-torrent-downloader .` rebuilds the Transmission image (optional).

## Coding Style & Naming Conventions
- Python: 4-space indentation, snake_case for functions/variables, ALL_CAPS for constants.
- Keep functions small and focused; avoid non-obvious side effects.
- Use standard library modules when possible; third-party deps are avoided.

## Testing Guidelines
- There are no automated tests in this repo.
- Manual checks: run `python3 vpngate.py`, confirm `vpngate.ovpn` updates, then `docker-compose up -d` and verify services start.

## Commit & Pull Request Guidelines
- Commit messages are short, imperative phrases (e.g., "Show country code in output").
- PRs should include: a brief description, the motivation for the change, and any config updates (e.g., `docker-compose.override.yml` or `transmission-settings.json`).
- If behavior changes, include example commands or log snippets.

## Security & Configuration Tips
- Do not commit personal VPN configs or credentials; keep secrets in local overrides.
- Verify `downloads/` and any mounted media paths are correct and writable for your UID/GID.
- `vpngate.ovpn` is regenerated; treat it as a derived artifact.
