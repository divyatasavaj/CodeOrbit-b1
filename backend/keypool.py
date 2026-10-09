"""Manage the Groq key pool in ``backend/.env``.

Usage::

    python3.13 keypool.py set  gsk_a,gsk_b,gsk_c,gsk_d,gsk_e,gsk_f
    python3.13 keypool.py check          # live-probes every key, no keys printed

``set`` writes ``GROQ_API_KEYS`` (comma-separated, order preserved) and clears
the single-key fallback so the pool is unambiguous. ``check`` reports HTTP
status and the remaining per-minute / per-day budget for each key so you can
tell a dead key from a throttled one before blaming the generator.
"""
import argparse
import os
import sys
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"
ENV_KEY = "GROQ_API_KEYS"


def _read_env() -> dict:
    values = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            name, _, value = stripped.partition("=")
            values[name.strip()] = value.strip()
    return values


def _write_env_key(name: str, value: str) -> None:
    """Replace ``name=`` in .env, appending it if the line is absent."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{name}=") and not line.strip().startswith("#"):
            lines[index] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n")


def cmd_set(raw: str) -> int:
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        print("no keys supplied", file=sys.stderr)
        return 2
    _write_env_key(ENV_KEY, ",".join(keys))
    _write_env_key("GROQ_API_KEY", "")
    print(f"wrote {len(keys)} keys to {ENV_PATH} (GROQ_API_KEYS)")
    print("restart the backend to pick them up: ")
    print("  /opt/anaconda3/bin/python3.13 -u main.py")
    return 0


def cmd_check() -> int:
    import httpx

    env = _read_env()
    raw = env.get(ENV_KEY) or env.get("GROQ_API_KEY") or ""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        print("no GROQ_API_KEYS / GROQ_API_KEY configured", file=sys.stderr)
        return 2

    model = env.get("GROQ_MODEL", "openai/gpt-oss-120b")
    url = "https://api.groq.com/openai/v1/chat/completions"
    print(f"probing {len(keys)} key(s) against {model}\n")
    healthy = 0
    for index, key in enumerate(keys, start=1):
        try:
            resp = httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "Reply with OK"}],
                    "max_tokens": 5,
                },
                timeout=20.0,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  key #{index} ...{key[-4:]}  NETWORK ERROR: {exc}")
            continue

        tag = "OK " if resp.status_code == 200 else "ERR"
        if resp.status_code == 200:
            healthy += 1
        limits = {
            "req/min left": resp.headers.get("x-ratelimit-remaining-requests"),
            "tok/min left": resp.headers.get("x-ratelimit-remaining-tokens"),
            "tok/day left": resp.headers.get("x-ratelimit-remaining-tokens-day"),
        }
        detail = ", ".join(f"{k}={v}" for k, v in limits.items() if v is not None)
        print(f"  key #{index} ...{key[-4:]}  {tag} HTTP {resp.status_code}  {detail}")
        if resp.status_code != 200:
            print(f"       {resp.text[:180]}")

    print(f"\n{healthy}/{len(keys)} keys healthy")
    return 0 if healthy else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the Groq key pool in backend/.env")
    sub = parser.add_subparsers(dest="command", required=True)
    set_parser = sub.add_parser("set", help="write GROQ_API_KEYS from a comma-separated list")
    set_parser.add_argument("keys", help="key1,key2,key3,key4,key5,key6")
    sub.add_parser("check", help="live-probe every configured key")
    args = parser.parse_args()
    if args.command == "set":
        return cmd_set(args.keys)
    return cmd_check()


if __name__ == "__main__":
    raise SystemExit(main())
