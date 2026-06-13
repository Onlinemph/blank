#!/usr/bin/env python3
"""
load_to_sqlite.py — Load the mul_scrape.py CSV outputs into a SQLite database.

Reads units.csv, factions.csv, types.csv, and availability.csv from an output
directory and builds a normalized, indexed SQLite file you can query with SQL.
Stdlib only — no pip install needed.

USAGE
-----
    python load_to_sqlite.py                       # reads ./out, writes ./mul.sqlite
    python load_to_sqlite.py --out out --db mul.sqlite
    python load_to_sqlite.py --db mul.sqlite --query \\
        "SELECT unit_name FROM v_availability WHERE faction='Federated Suns' AND era='Jihad'"

TABLES
------
    units         unit_id, unit_name, class, variant, type_id, type, tonnage, bv2, intro
    factions      faction_id, faction
    types         type_id, type
    eras          era_id, era
    availability  faction_id, era_id, unit_id   (the join table)

VIEW
----
    v_availability  availability joined to human-readable names + unit details

EXAMPLE QUERIES
---------------
    -- What can the Draconis Combine field in the Clan Invasion era?
    SELECT unit_name, tonnage FROM v_availability
    WHERE faction='Draconis Combine' AND era='Clan Invasion'
    ORDER BY tonnage;

    -- How many factions can field each unit in the Jihad?
    SELECT unit_name, COUNT(*) AS factions FROM v_availability
    WHERE era='Jihad' GROUP BY unit_id ORDER BY factions DESC;

    -- Every era a specific unit shows up in, and who fields it:
    SELECT era, GROUP_CONCAT(faction, '; ') AS factions
    FROM v_availability WHERE unit_name LIKE 'Warhammer WHM-7A'
    GROUP BY era_id;
"""

import argparse
import csv
import os
import sqlite3
import sys


def read_csv(path):
    if not os.path.exists(path):
        return None
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def build(out_dir, db_path):
    units = read_csv(os.path.join(out_dir, "units.csv"))
    avail = read_csv(os.path.join(out_dir, "availability.csv"))
    factions = read_csv(os.path.join(out_dir, "factions.csv"))
    types = read_csv(os.path.join(out_dir, "types.csv"))

    if avail is None or units is None:
        sys.exit(f"Missing units.csv/availability.csv in {out_dir!r}. "
                 f"Run mul_scrape.py first.")

    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE units (
            unit_id INTEGER PRIMARY KEY, unit_name TEXT, class TEXT, variant TEXT,
            type_id INTEGER, type TEXT, tonnage REAL, bv2 INTEGER, intro TEXT
        );
        CREATE TABLE factions (faction_id INTEGER PRIMARY KEY, faction TEXT);
        CREATE TABLE types    (type_id INTEGER PRIMARY KEY, type TEXT);
        CREATE TABLE eras     (era_id INTEGER PRIMARY KEY, era TEXT);
        CREATE TABLE availability (
            faction_id INTEGER, era_id INTEGER, unit_id INTEGER,
            PRIMARY KEY (faction_id, era_id, unit_id)
        );
    """)

    cur.executemany(
        "INSERT OR REPLACE INTO units VALUES (?,?,?,?,?,?,?,?,?)",
        [(to_int(u["unit_id"]), u["unit_name"], u.get("class"), u.get("variant"),
          to_int(u.get("type_id")), u.get("type"), to_int(u.get("tonnage")),
          to_int(u.get("bv")), u.get("intro")) for u in units],
    )

    if factions:
        cur.executemany(
            "INSERT OR REPLACE INTO factions VALUES (?,?)",
            [(to_int(r["faction_id"]), r["faction"]) for r in factions],
        )
    if types:
        cur.executemany(
            "INSERT OR REPLACE INTO types VALUES (?,?)",
            [(to_int(r["type_id"]), r["type"]) for r in types],
        )

    # eras + availability derived from the long table
    eras = {}
    rows = []
    for a in avail:
        eid = to_int(a["era_id"])
        eras[eid] = a["era"]
        rows.append((to_int(a["faction_id"]), eid, to_int(a["unit_id"])))
    cur.executemany("INSERT OR REPLACE INTO eras VALUES (?,?)",
                    sorted(eras.items()))
    cur.executemany("INSERT OR IGNORE INTO availability VALUES (?,?,?)", rows)

    cur.executescript("""
        CREATE INDEX ix_av_faction ON availability(faction_id);
        CREATE INDEX ix_av_era     ON availability(era_id);
        CREATE INDEX ix_av_unit    ON availability(unit_id);
        CREATE INDEX ix_units_type ON units(type_id);
        DROP VIEW IF EXISTS v_availability;
        CREATE VIEW v_availability AS
        SELECT a.faction_id, f.faction, a.era_id, e.era,
               a.unit_id, u.unit_name, u.type, u.tonnage, u.bv2, u.intro
        FROM availability a
        LEFT JOIN factions f ON f.faction_id = a.faction_id
        LEFT JOIN eras     e ON e.era_id     = a.era_id
        LEFT JOIN units    u ON u.unit_id    = a.unit_id;
    """)
    con.commit()

    n = lambda t: cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"Built {db_path}")
    print(f"  units        {n('units')}")
    print(f"  factions     {n('factions')}")
    print(f"  eras         {n('eras')}")
    print(f"  types        {n('types')}")
    print(f"  availability {n('availability')} rows")
    return con


def main():
    ap = argparse.ArgumentParser(description="Load MUL CSVs into SQLite.")
    ap.add_argument("--out", default="out", help="dir holding the scraper CSVs")
    ap.add_argument("--db", default="mul.sqlite", help="SQLite file to create")
    ap.add_argument("--query", help="run a SQL query against the new DB and print results")
    args = ap.parse_args()

    con = build(args.out, args.db)
    if args.query:
        print("\n" + args.query)
        cur = con.execute(args.query)
        cols = [d[0] for d in cur.description] if cur.description else []
        if cols:
            print(" | ".join(cols))
        for row in cur.fetchall():
            print(" | ".join("" if v is None else str(v) for v in row))
    con.close()


if __name__ == "__main__":
    main()
