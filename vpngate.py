#!/usr/bin/env python3
import argparse
import base64
import csv
import json
import os
import pathlib
import re
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone


API_URL = "https://www.vpngate.net/api/iphone/"
DEFAULT_OUTPUT = pathlib.Path("gluetun/vpngate.ovpn")
DEFAULT_STATE = pathlib.Path("gluetun/vpngate-state.json")
DEFAULT_MIN_SPEED = 5_000_000
DEFAULT_MAX_PING = 300
DEFAULT_MIN_UPTIME = 86_400


def load_env(path):
    values = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def config_value(env_file_values, key, default=None):
    return os.environ.get(key) or env_file_values.get(key) or default


def parse_int(value, default):
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def int_field(row, field, default=0):
    value = row.get(field)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def fetch_servers(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        text = response.read().decode("utf-8", errors="replace")

    lines = [line for line in text.splitlines() if line and not line.startswith("*")]
    if not lines:
        raise RuntimeError("VPNGate returned no CSV data")

    header = lines[0].lstrip("#")
    rows = csv.DictReader([header, *lines[1:]])
    servers = [row for row in rows if row.get("OpenVPN_ConfigData_Base64")]
    if not servers:
        raise RuntimeError("VPNGate returned no OpenVPN configs")
    return servers


def choose_server(servers):
    def score(server):
        return (
            int_field(server, "Score"),
            int_field(server, "Speed"),
            -int_field(server, "Ping", 999999),
        )

    return max(servers, key=score)


def validate_config(config):
    required = ["client", "dev tun", "<ca>", "</ca>"]
    missing = [marker for marker in required if marker not in config]
    if missing:
        raise RuntimeError(
            "VPNGate OpenVPN config is missing expected marker(s): "
            + ", ".join(missing)
        )


def ensure_server_certificate_verification(config):
    if re.search(
        r"^(remote-cert-tls|verify-x509-name|tls-remote|ns-cert-type)\b",
        config,
        flags=re.MULTILINE,
    ):
        return config
    return re.sub(
        r"^(client\s*)$",
        r"\1\nremote-cert-tls server",
        config,
        count=1,
        flags=re.MULTILINE,
    )


def filter_servers(servers, country, min_speed, max_ping, min_uptime):
    candidates = servers
    if country:
        wanted = country.upper()
        candidates = [
            server for server in candidates
            if server.get("CountryShort", "").upper() == wanted
            or server.get("CountryLong", "").upper() == wanted
        ]
        if not candidates:
            raise RuntimeError(f"VPNGate returned no OpenVPN configs for {country}")

    filtered = [
        server for server in candidates
        if int_field(server, "Speed") >= min_speed
        and int_field(server, "Ping", 999999) <= max_ping
        and int_field(server, "Uptime") >= min_uptime
    ]
    if not filtered:
        raise RuntimeError(
            "VPNGate returned no configs after filtering "
            f"(country={country or 'any'}, min_speed={min_speed}, "
            f"max_ping={max_ping}, min_uptime={min_uptime})"
        )
    return filtered


def write_config(server, output):
    config = base64.b64decode(server["OpenVPN_ConfigData_Base64"]).decode("utf-8")
    validate_config(config)
    config = ensure_server_certificate_verification(config)
    write_text_atomic(output, config)


def write_state(server, state_path, output):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output": str(output),
        "host_name": server.get("HostName"),
        "ip": server.get("IP"),
        "country_short": server.get("CountryShort"),
        "country_long": server.get("CountryLong"),
        "score": int_field(server, "Score"),
        "speed": int_field(server, "Speed"),
        "ping": int_field(server, "Ping", 999999),
        "uptime": int_field(server, "Uptime"),
        "total_users": int_field(server, "TotalUsers"),
    }
    write_text_atomic(state_path, json.dumps(state, indent=2) + "\n")


def write_text_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_file.write(text)
        temp_name = temp_file.name
    os.replace(temp_name, path)


def main():
    parser = argparse.ArgumentParser(
        description="Generate a Gluetun OpenVPN config from VPNGate."
    )
    parser.add_argument(
        "--country",
        help="Optional VPNGate country name or two-letter country code. Defaults to VPNGATE_COUNTRY from .env.",
    )
    parser.add_argument(
        "--env-file",
        type=pathlib.Path,
        default=pathlib.Path(".env"),
        help="Optional env file for VPNGATE_* settings. Default: .env",
    )
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=DEFAULT_OUTPUT,
        help=f"Output OpenVPN config path. Default: {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--state",
        type=pathlib.Path,
        default=DEFAULT_STATE,
        help=f"State file path. Default: {DEFAULT_STATE}",
    )
    parser.add_argument(
        "--min-speed",
        type=int,
        help=f"Minimum VPNGate speed in bits per second. Default: {DEFAULT_MIN_SPEED}",
    )
    parser.add_argument(
        "--max-ping",
        type=int,
        help=f"Maximum VPNGate ping in ms. Default: {DEFAULT_MAX_PING}",
    )
    parser.add_argument(
        "--min-uptime",
        type=int,
        help=f"Minimum VPNGate uptime in seconds. Default: {DEFAULT_MIN_UPTIME}",
    )
    args = parser.parse_args()

    env_file_values = load_env(args.env_file)
    country = args.country or config_value(env_file_values, "VPNGATE_COUNTRY")
    min_speed = args.min_speed if args.min_speed is not None else parse_int(
        config_value(env_file_values, "VPNGATE_MIN_SPEED"),
        DEFAULT_MIN_SPEED,
    )
    max_ping = args.max_ping if args.max_ping is not None else parse_int(
        config_value(env_file_values, "VPNGATE_MAX_PING"),
        DEFAULT_MAX_PING,
    )
    min_uptime = args.min_uptime if args.min_uptime is not None else parse_int(
        config_value(env_file_values, "VPNGATE_MIN_UPTIME"),
        DEFAULT_MIN_UPTIME,
    )

    servers = fetch_servers(API_URL)
    candidates = filter_servers(servers, country, min_speed, max_ping, min_uptime)
    server = choose_server(candidates)
    write_config(server, args.output)
    write_state(server, args.state, args.output)

    country = server.get("CountryShort") or server.get("CountryLong") or "unknown"
    host = server.get("HostName") or server.get("IP") or "unknown"
    print(f"Wrote {args.output} from {host} ({country})")
    print(f"Wrote {args.state}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
