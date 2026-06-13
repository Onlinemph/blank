# Master Unit List — Faction/Era Availability Scraper

Pulls "which units are available to which faction in which era" from the
**Master Unit List** (masterunitlist.info) — the canonical BattleTech unit
database maintained by Catalyst Game Labs, and the same source the Sarna wiki
defers to for availability. It then loads the result into a queryable SQLite DB.

The MUL has no official public API. These scripts use its backend `QuickList`
JSON endpoint (the one the site's own search UI runs on). It's unofficial, so
be polite: run once, keep the default delay or slower, and cache the results.

## Files

| File | What it does | Deps |
|------|--------------|------|
| `mul_scrape.py` | Scrapes the endpoint, writes CSVs to `./out/` | `requests` |
| `load_to_sqlite.py` | Loads those CSVs into `mul.sqlite` | stdlib only |

## Quick start

```bash
pip install requests

# 1. (optional) see the unit Type id -> name map
python mul_scrape.py --list-types

# 2. full scrape -> writes ./out/*.csv  (~15-25 min at default delay)
python mul_scrape.py

# 3. build the database from ./out
python load_to_sqlite.py            # -> ./mul.sqlite
```

If interrupted, rerun `python mul_scrape.py --resume` to continue — it
checkpoints after every faction.

## Outputs (in `./out/`)

- `units.csv` — one row per unique unit (id, name, class, variant, type, tonnage, BV, intro year)
- `availability.csv` — long/relational table: one row per faction × era × unit
- `availability_matrix.csv` — wide pivot: unit rows, era columns, factions listed per cell
- `factions.csv` — discovered faction id → name map
- `types.csv` — discovered unit-type id → name map
- `availability.jsonl` — raw append log (used by `--resume`)

## Database

`load_to_sqlite.py` builds normalized tables (`units`, `factions`, `eras`,
`types`, `availability`) plus a convenience view **`v_availability`** that joins
everything to readable names. Query the view and you can mostly ignore the IDs.

```bash
# inline query straight from the loader
python load_to_sqlite.py --query \
  "SELECT unit_name, tonnage FROM v_availability
   WHERE faction='Draconis Combine' AND era='Clan Invasion'
   ORDER BY tonnage"
```

More examples:

```sql
-- How many factions can field each unit in the Jihad?
SELECT unit_name, COUNT(*) AS factions FROM v_availability
WHERE era='Jihad' GROUP BY unit_id ORDER BY factions DESC;

-- Every era a unit appears in, and who fields it:
SELECT era, GROUP_CONCAT(faction, '; ') AS factions
FROM v_availability WHERE unit_name='Warhammer WHM-7A'
GROUP BY era_id;
```

## Read this before trusting the numbers

**Era IDs are the soft spot.** They're hard-coded at the top of `mul_scrape.py`
from community documentation and *validated at startup* against the live site.
If the validator flags any (most likely Civil War `247` or the Late Succession
War split `255`/`256`), open the era filter dropdown on masterunitlist.info,
read the real IDs, and fix the `ERAS` dict. "Late Republic" was left out because
its ID was ambiguous in the sources — add it the same way if you need it.

**Possible result cap.** It couldn't be confirmed whether the endpoint caps a
single response. If any faction/era bucket comes back a suspiciously round size
(exactly 200, 500, …), rerun with `--split-tons` — it recursively bisects that
bucket by tonnage and merges the pieces.

**Blank ≠ confirmed unavailable.** An empty matrix cell / missing row means "not
recorded as available," which lumps together genuine unavailability and gaps in
the upstream MUL data. Per the MUL itself, ilClan-era availability is still beta,
and some "off-screen" factions (e.g. post-3081 Homeworld Clans, surviving Word of
Blake/Wolverine remnants) are deliberately omitted. That's upstream, not the scraper.

## Useful flags (`mul_scrape.py`)

- `--delay 0.6` — seconds between requests (raise to be gentler)
- `--max-faction-id 600` — widen the faction-id scan (default 500)
- `--types 18` — restrict to one unit type (18 = BattleMech; see `types.csv`)
- `--split-tons` — subdivide capped buckets by tonnage
- `--resume` — continue an interrupted run
- `--out DIR` — change the output directory

## How it works (for whoever maintains this)

Faction IDs aren't listed by any endpoint, so the scraper scans IDs `1..max` and
keeps only the ones the server echoes back as a real faction in the response's
`Crumbs` field — that's the guard against an invalid ID silently returning the
whole catalog. Names and unit types come straight from the records, so there's
no hand-maintained list to drift out of date. Eras are the one fixed table, which
is why they get the startup validation pass.
