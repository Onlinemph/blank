#!/usr/bin/env python3
"""
mul_scrape.py — Scrape the Master Unit List (masterunitlist.info) availability data.

WHAT IT DOES
------------
For every faction and every era, it queries the MUL's backend QuickList endpoint
(the same JSON the website's search UI runs on) and records which units are
available to which faction in which era. It writes three files:

    out/units.csv              one row per unique unit (id, name, type, tonnage, BV, intro)
    out/availability.csv       long/relational table: faction x era x unit
    out/availability_matrix.csv wide pivot: unit rows, era columns, factions per cell
    out/factions.csv           the faction id -> name map it discovered
    out/types.csv              the unit-type id -> name map it discovered
    out/availability.jsonl     raw append log (used for --resume)

The endpoint is undocumented and unofficial. Be polite: keep the delay reasonable,
don't hammer it, and cache your results so you only scrape once.

USAGE
-----
    python mul_scrape.py                      # full scrape, defaults
    python mul_scrape.py --list-types         # just print the Type id -> name map
    python mul_scrape.py --delay 0.6          # slower, gentler on the server
    python mul_scrape.py --max-faction-id 600 # widen the faction-id scan
    python mul_scrape.py --resume             # continue an interrupted run
    python mul_scrape.py --split-tons         # subdivide huge result sets by tonnage
    python mul_scrape.py --types 18           # restrict to BattleMechs only

NOTES
-----
* Faction IDs are discovered by scanning 1..MAX and keeping the ones the server
  echoes back as a real faction in the response "Crumbs". Invalid IDs cost one
  request each and are skipped.
* ERA IDs are hard-coded below from community documentation. They are validated
  at startup against the live endpoint; any that don't echo back an Era crumb are
  reported so you can fix them against the site's filter dropdown.
* If you suspect the endpoint caps a result set (a faction/era bucket comes back
  suspiciously round, e.g. exactly 200 or 500), rerun with --split-tons; it will
  recursively bisect that bucket by tonnage and merge the pieces.
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict

import requests

BASE = "https://masterunitlist.azurewebsites.net/Unit/QuickList"

# --- Era id -> name. From community docs; validated at runtime. Fix here if the ---
# --- startup validator flags one (compare against the site's Era filter dropdown).
ERAS = {
    10:  "Age of War / Star League",
    11:  "Early Succession War",
    255: "Late Succession War - LosTech",
    256: "Late Succession War - Renaissance",
    13:  "Clan Invasion",
    247: "Civil War",
    14:  "Jihad",
    15:  "Early Republic",
    16:  "Dark Age",
    257: "ilClan",
}

HEADERS = {
    "User-Agent": "mul-availability-scraper/1.0 (personal research; contact: you@example.com)",
    "Accept": "application/json",
}


def make_session(retries: int = 4) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(
            total=retries,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        s.mount("https://", HTTPAdapter(max_retries=retry))
    except Exception:
        pass
    return s


def quicklist(session: requests.Session, delay: float, **params) -> dict:
    """One GET against QuickList. Returns parsed JSON ({} on hard failure)."""
    # drop None values so we don't send empty params
    params = {k: v for k, v in params.items() if v is not None}
    for attempt in range(5):
        try:
            r = session.get(BASE, params=params, timeout=30)
            r.raise_for_status()
            time.sleep(delay)
            return r.json()
        except Exception as e:
            wait = 2 ** attempt
            print(f"    ! request failed ({e}); retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    return {}


def crumb_value(data: dict, key_substr: str):
    """Pull a value out of the response 'Crumbs' (the echoed filter description)."""
    for c in data.get("Crumbs", []) or []:
        if key_substr.lower() in str(c.get("Name", "")).lower():
            return c.get("Value")
    return None


def units_in(data: dict) -> list:
    return data.get("Units", []) or []


def fetch_bucket(session, delay, faction_id, era_id, types, split_tons,
                 min_tons=None, max_tons=None, cap=400, depth=0):
    """
    Fetch all units for (faction, era[, type]) and optionally subdivide by tonnage
    if the result set looks capped. Returns (faction_name_or_None, [unit dicts]).
    """
    data = quicklist(
        session, delay,
        Factions=faction_id, AvailableEras=era_id, Types=types,
        MinTons=min_tons, MaxTons=max_tons,
    )
    fac_name = crumb_value(data, "faction")
    units = units_in(data)

    # Invalid faction id: server didn't echo a faction crumb. Bail.
    if fac_name is None:
        return None, []

    # Adaptive tonnage subdivision when a bucket looks capped.
    if split_tons and len(units) >= cap and depth < 8:
        lo = min_tons if min_tons is not None else 0
        hi = max_tons if max_tons is not None else 200
        if hi - lo > 1:
            mid = (lo + hi) // 2
            _, a = fetch_bucket(session, delay, faction_id, era_id, types,
                                split_tons, lo, mid, cap, depth + 1)
            _, b = fetch_bucket(session, delay, faction_id, era_id, types,
                                split_tons, mid + 1, hi, cap, depth + 1)
            merged = {u["Id"]: u for u in (a + b)}
            return fac_name, list(merged.values())
        print(f"    ! bucket faction={faction_id} era={era_id} tons={lo}-{hi} "
              f"still at cap ({len(units)}); cannot split further", file=sys.stderr)

    return fac_name, units


def list_types(session, delay):
    """Best-effort discovery of Type id -> name without running a full scrape.
    Probes one query per era (no faction filter) and collects distinct Types.
    Result sets may be capped, but each type typically appears at least once."""
    seen = {}
    print("Discovering unit Type ids (best-effort)...")
    for eid, ename in ERAS.items():
        data = quicklist(session, delay, AvailableEras=eid)
        for u in units_in(data):
            t = u.get("Type") or {}
            if t.get("Id") is not None:
                seen[t["Id"]] = t.get("Name")
    if not seen:
        print("  (nothing found — the no-faction query may be restricted; "
              "run a full scrape and read out/types.csv instead)")
    for tid in sorted(seen):
        print(f"  {tid:>3} = {seen[tid]}")
    print("\nUse e.g. --types 18 to restrict the scrape to a single type.")
    return seen


def validate_eras(session, delay):
    print("Validating era IDs against the live endpoint...")
    bad = []
    for eid, name in ERAS.items():
        data = quicklist(session, delay, AvailableEras=eid, Name="Atlas")
        echoed = crumb_value(data, "era")
        status = "ok" if echoed else "NO ERA CRUMB"
        print(f"  era {eid:>3} ({name}): {status}"
              + (f"  -> server says: {echoed}" if echoed else ""))
        if not echoed:
            bad.append((eid, name))
    if bad:
        print("\n  WARNING: the above era IDs did not validate. Compare them to the\n"
              "  era filter dropdown on masterunitlist.info and fix the ERAS dict.\n")
    return bad


def main():
    ap = argparse.ArgumentParser(description="Scrape MUL faction/era unit availability.")
    ap.add_argument("--delay", type=float, default=0.4, help="seconds between requests")
    ap.add_argument("--max-faction-id", type=int, default=500, help="highest faction id to scan")
    ap.add_argument("--min-faction-id", type=int, default=1)
    ap.add_argument("--types", default=None, help="restrict to a Type id (e.g. 18 = BattleMech)")
    ap.add_argument("--split-tons", action="store_true", help="subdivide capped buckets by tonnage")
    ap.add_argument("--list-types", action="store_true", help="just discover Type ids and exit")
    ap.add_argument("--cap", type=int, default=400, help="bucket size that triggers a split")
    ap.add_argument("--resume", action="store_true", help="continue from existing out/ files")
    ap.add_argument("--out", default="out", help="output directory")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    jsonl_path = os.path.join(args.out, "availability.jsonl")
    ckpt_path = os.path.join(args.out, "checkpoint.json")

    session = make_session()
    if args.list_types:
        list_types(session, args.delay)
        return
    validate_eras(session, args.delay)

    done_factions = set()
    if args.resume and os.path.exists(ckpt_path):
        with open(ckpt_path) as f:
            done_factions = set(json.load(f).get("done_factions", []))
        print(f"Resuming; {len(done_factions)} faction ids already done.")

    jsonl = open(jsonl_path, "a" if args.resume else "w", encoding="utf-8")

    factions = {}            # id -> name
    print(f"\nScanning faction ids {args.min_faction_id}..{args.max_faction_id}")
    for fid in range(args.min_faction_id, args.max_faction_id + 1):
        if fid in done_factions:
            continue
        fac_name_seen = None
        total_rows = 0
        for eid, ename in ERAS.items():
            fac_name, units = fetch_bucket(
                session, args.delay, fid, eid, args.types,
                args.split_tons, cap=args.cap,
            )
            if fac_name is None:
                # invalid faction id -> stop probing further eras for it
                break
            fac_name_seen = fac_name
            for u in units:
                row = {
                    "faction_id": fid,
                    "faction": fac_name,
                    "era_id": eid,
                    "era": ename,
                    "unit_id": u.get("Id"),
                    "unit_name": u.get("Name"),
                    "class": u.get("Class"),
                    "variant": u.get("Variant"),
                    "type_id": (u.get("Type") or {}).get("Id"),
                    "type": (u.get("Type") or {}).get("Name"),
                    "tonnage": u.get("Tonnage"),
                    "bv": u.get("BattleValue"),
                    "intro": u.get("DateIntroduced"),
                }
                jsonl.write(json.dumps(row) + "\n")
                total_rows += 1
        if fac_name_seen:
            factions[fid] = fac_name_seen
            print(f"  faction {fid:>3} = {fac_name_seen:<35} {total_rows} unit-rows")
        done_factions.add(fid)
        # checkpoint every faction so --resume works after a Ctrl-C
        with open(ckpt_path, "w") as f:
            json.dump({"done_factions": sorted(done_factions)}, f)

    jsonl.close()
    print("\nScrape pass complete. Building CSVs...")
    build_csvs(jsonl_path, factions, args.out)
    print("Done. See:", args.out)


def build_csvs(jsonl_path, factions, out):
    units = {}
    avail = []
    types = {}
    # unit_id -> era_id -> set(faction names)
    matrix = defaultdict(lambda: defaultdict(set))
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            uid = r["unit_id"]
            if uid not in units:
                units[uid] = {
                    "unit_id": uid, "unit_name": r["unit_name"],
                    "class": r["class"], "variant": r["variant"],
                    "type_id": r.get("type_id"), "type": r["type"],
                    "tonnage": r["tonnage"], "bv": r["bv"], "intro": r["intro"],
                }
            if r.get("type_id") is not None:
                types[r["type_id"]] = r["type"]
            avail.append({
                "faction_id": r["faction_id"], "faction": r["faction"],
                "era_id": r["era_id"], "era": r["era"],
                "unit_id": uid, "unit_name": r["unit_name"],
            })
            matrix[uid][r["era_id"]].add(r["faction"])

    # units.csv
    with open(os.path.join(out, "units.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["unit_id", "unit_name", "class", "variant",
                                          "type_id", "type", "tonnage", "bv", "intro"])
        w.writeheader()
        for u in sorted(units.values(), key=lambda x: (x["unit_name"] or "")):
            w.writerow(u)

    # availability.csv (long / relational)
    with open(os.path.join(out, "availability.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["faction_id", "faction", "era_id", "era",
                                          "unit_id", "unit_name"])
        w.writeheader()
        for a in avail:
            w.writerow(a)

    # factions.csv (discovered id -> name)
    fac_from_log = {a["faction_id"]: a["faction"] for a in avail}
    fac_from_log.update(factions)
    with open(os.path.join(out, "factions.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["faction_id", "faction"])
        for fid in sorted(fac_from_log):
            w.writerow([fid, fac_from_log[fid]])

    # types.csv (resolved id -> name, harvested from the unit records)
    with open(os.path.join(out, "types.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["type_id", "type"])
        for tid in sorted(types):
            w.writerow([tid, types[tid]])

    # availability_matrix.csv (wide: one row per unit, one column per era,
    # each cell = the factions that can field that unit in that era)
    era_cols = list(ERAS.items())  # insertion order == chronological-ish
    with open(os.path.join(out, "availability_matrix.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["unit_id", "unit_name", "type", "tonnage"]
                   + [name for _, name in era_cols])
        for uid in sorted(units, key=lambda x: (units[x]["unit_name"] or "")):
            u = units[uid]
            cells = ["; ".join(sorted(matrix[uid].get(eid, ()))) for eid, _ in era_cols]
            w.writerow([uid, u["unit_name"], u["type"], u["tonnage"]] + cells)

    print(f"  units.csv               {len(units)} unique units")
    print(f"  availability.csv        {len(avail)} faction-era-unit rows")
    print(f"  availability_matrix.csv {len(units)} units x {len(era_cols)} eras")
    print(f"  factions.csv            {len(fac_from_log)} factions")
    print(f"  types.csv               {len(types)} unit types")


if __name__ == "__main__":
    main()
