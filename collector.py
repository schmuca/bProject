#!/usr/bin/env python3
"""Poll adsb.lol v2 API for Allegiant Air fleet hex codes and append to daily JSONL."""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = "https://api.adsb.lol/v2/hex/"
REGISTRY_PATH = Path("fleet_registry.csv")
OUTPUT_DIR = Path("data/raw")
BATCH_SIZE = 100  # Keeps URL well under HTTP length limits (125 jets = 2 requests)


def load_active_hexes(path: Path) -> dict[str, dict]:
    """Return {icao_hex_lower: metadata} for all ACTIVE aircraft in fleet_registry.csv."""
    fleet: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status", "ACTIVE").strip().upper() != "ACTIVE":
                continue
            hex_code = row["icao_hex"].strip().lower()
            if hex_code:
                fleet[hex_code] = {
                    "registration": row.get("registration", "").strip(),
                    "aircraft_type": row.get("aircraft_type", "").strip(),
                    "seat_count": int(row["seat_count"]) if row.get("seat_count") else None,
                }
    return fleet


def fetch_batch(hex_codes: list[str], api_key: str | None) -> list[dict]:
    """Query adsb.lol for a comma-separated batch of ICAO hex codes."""
    url = API_BASE + ",".join(hex_codes)
    headers = {
        "User-Agent": "allegiant-adsb-tracker/1.0 (research PoC)",
        "Accept": "application/json",
    }
    if api_key:
        headers["x-api-key"] = api_key

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("ac", [])
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = e.headers.get("Retry-After", "unknown")
            print(f"[WARN] Rate limited (429). Retry-After: {retry_after}", file=sys.stderr)
        else:
            print(f"[ERROR] HTTP {e.code} for batch: {e.reason}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[ERROR] Request failed: {e}", file=sys.stderr)
        return []


def normalize_ping(ac: dict, ts_utc: str, fleet: dict[str, dict]) -> dict:
    """Extract only the telemetry fields needed for flight & ASK reconstruction."""
    hex_code = str(ac.get("hex", "")).strip().lower()
    alt_baro = ac.get("alt_baro")
    on_ground = alt_baro == "ground"
    meta = fleet.get(hex_code, {})

    return {
        "ts_utc": ts_utc,
        "hex": hex_code,
        "reg": ac.get("r") or meta.get("registration"),
        "type": ac.get("t") or meta.get("aircraft_type"),
        "seats": meta.get("seat_count"),
        "callsign": (ac.get("flight") or "").strip(),
        "lat": ac.get("lat"),
        "lon": ac.get("lon"),
        "alt_baro": 0 if on_ground else alt_baro,
        "gs": ac.get("gs"),
        "track": ac.get("track"),
        "squawk": ac.get("squawk"),
        "on_ground": on_ground,
        "seen": ac.get("seen"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing to disk")
    args = parser.parse_args()

    if not REGISTRY_PATH.exists():
        sys.exit(f"Missing {REGISTRY_PATH}. Create it before running.")

    fleet = load_active_hexes(REGISTRY_PATH)
    hex_list = list(fleet.keys())
    api_key = os.getenv("ADSB_LOL_API_KEY") or None

    now = datetime.now(timezone.utc)
    ts_utc = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    records: list[dict] = []

    for i in range(0, len(hex_list), BATCH_SIZE):
        batch = hex_list[i : i + BATCH_SIZE]
        aircraft_list = fetch_batch(batch, api_key)
        for ac in aircraft_list:
            records.append(normalize_ping(ac, ts_utc, fleet))
        if i + BATCH_SIZE < len(hex_list):
            time.sleep(1)

    if args.dry_run:
        print(f"Fetched {len(records)} active aircraft out of {len(hex_list)} tracked at {ts_utc}:")
        for r in records[:10]:
            print(json.dumps(r))
        return

    if not records:
        print(f"[INFO] 0 aircraft returned at {ts_utc}; nothing written.")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUT_DIR / f"{now.strftime('%Y-%m-%d')}.jsonl"
    with out_file.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")

    print(f"[OK] Appended {len(records)} records to {out_file}")


if __name__ == "__main__":
    main()
