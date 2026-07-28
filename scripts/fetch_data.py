#!/usr/bin/env python3
"""
Pulls Texas barbering/cosmetology establishment license records from the
TDLR "All Licenses" dataset on data.texas.gov (Socrata), classifies them
into shop categories, geocodes their addresses via the U.S. Census Bureau's
free batch geocoder, and writes a compact JSON file with:
  - a county-level rollup (shop counts by category)
  - individual shop records with lat/lon, for the map

No API key is required for either the Socrata query or the Census
geocoder. This script is slower than the NY one because Texas doesn't
publish coordinates -- we have to geocode ~tens of thousands of addresses
ourselves, in batches, which can take a while. That's expected.

Usage:
    python scripts/fetch_data.py
"""
import csv
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import requests  # only needed for the geocoder's multipart file upload

RESOURCE_ID = "7358-krk7"  # "TDLR - All Licenses"
METADATA_URL = f"https://data.texas.gov/api/views/{RESOURCE_ID}.json"
BASE_URL = f"https://data.texas.gov/resource/{RESOURCE_ID}.json"
GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"

PAGE_SIZE = 5000
GEOCODE_BATCH_SIZE = 1000        # smaller batches: the real run showed the
                                  # Census geocoder throwing frequent 502s and
                                  # timeouts under load -- a failed small batch
                                  # loses less than a failed big one.
GEOCODE_PAUSE_SECONDS = 2.0
GEOCODE_REQUEST_TIMEOUT = 120     # fail faster and retry, rather than hang
GEOCODE_MAX_RETRIES = 4
GEOCODE_RETRY_BACKOFF_SECONDS = [5, 15, 45, 90]
CHECKPOINT_EVERY_N_BATCHES = 5    # every ~5,000 addresses

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "tx_shops.json")

# Human column headers we need, as they appear in the TDLR CSV export.
# We look up their actual Socrata API field names at runtime (see
# get_field_map) rather than hardcoding guesses, since Socrata's
# auto-generated field IDs aren't always predictable from the header text.
NEEDED_COLUMNS = [
    "LICENSE TYPE",
    "LICENSE SUBTYPE",
    "BUSINESS NAME",
    "BUSINESS ADDRESS-LINE1",
    "BUSINESS ADDRESS-LINE2",
    "BUSINESS CITY, STATE ZIP",
    "BUSINESS COUNTY",
]

CATEGORY_LABELS = {
    "BARBER": "Barber Shop (legacy label)",
    "SALON": "Beauty / Cosmetology Salon (legacy label)",
    "UNSPLIT": "Full-Service / Dual / Specialty Establishment",
    "UNKNOWN": "Unclassified (flagged for review)",
}

CSZ_RE = re.compile(r"^\s*(.*?),\s*([A-Z]{2})(?:\s+(\d{5})(?:-\d{4})?)?\s*$")


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tx-shop-directory/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")
        raise RuntimeError(f"data.texas.gov request failed: {e.code} {body[:300]} ({url})")


def get_field_map():
    """Ask Socrata for this dataset's real column->fieldName mapping."""
    meta = _get_json(METADATA_URL)
    by_name = {c["name"]: c["fieldName"] for c in meta.get("columns", [])}
    missing = [c for c in NEEDED_COLUMNS if c not in by_name]
    if missing:
        raise RuntimeError(f"Expected columns not found in TDLR dataset metadata: {missing}")
    return by_name


def fetch_all_records(fields):
    lt, sub, bn, a1, a2, csz, county = (
        fields["LICENSE TYPE"], fields["LICENSE SUBTYPE"], fields["BUSINESS NAME"],
        fields["BUSINESS ADDRESS-LINE1"], fields["BUSINESS ADDRESS-LINE2"],
        fields["BUSINESS CITY, STATE ZIP"], fields["BUSINESS COUNTY"],
    )
    select_fields = ",".join([lt, sub, bn, a1, a2, csz, county])
    # Keyword filter server-side to avoid pulling all ~960k TDLR rows across
    # every trade it regulates -- we only want shop/salon/establishment-type
    # licenses. Anything this misses or over-includes gets sorted out in
    # classify() below, and unrecognized types are logged, not dropped silently.
    where = (
        f"(upper({lt}) like '%SHOP%' or upper({lt}) like '%SALON%' "
        f"or upper({lt}) like '%ESTABLISHMENT%')"
    )

    records = []
    offset = 0
    while True:
        params = {
            "$select": select_fields,
            "$where": where,
            "$limit": PAGE_SIZE,
            "$offset": offset,
            "$order": ":id",
        }
        url = BASE_URL + "?" + urllib.parse.urlencode(params)
        batch = _get_json(url)
        if not batch:
            break
        records.extend(batch)
        print(f"  fetched {len(records)} records so far...")
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return records


def classify(license_type):
    t = (license_type or "").upper()
    if "SCHOOL" in t or "INSTRUCTOR" in t:
        return None  # education, not a shop -- excluded entirely
    if "ESTABLISHMENT" in t or "DUAL" in t or "SPECIALTY" in t:
        return "UNSPLIT"
    if "BARBER" in t and "SHOP" in t:
        return "BARBER"
    if ("BEAUTY" in t or "COSMETOLOGY" in t) and ("SHOP" in t or "SALON" in t):
        return "SALON"
    return "UNKNOWN"


def parse_csz(value):
    if not value:
        return (None, None, None)
    m = CSZ_RE.match(value.strip())
    if not m:
        return (value.strip() or None, None, None)
    return (m.group(1) or None, m.group(2), m.group(3))


def geocode_key(address, city, zip5):
    return f"{(address or '').strip().upper()}|{(city or '').strip().upper()}|{(zip5 or '').strip()}"


def load_previous_geocode_cache():
    """Reuse lat/lon from a prior committed run so re-runs (or resumed runs
    after a timeout) don't have to re-geocode addresses we already solved."""
    if not os.path.exists(OUT_PATH):
        return {}
    try:
        with open(OUT_PATH) as f:
            prev = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    cache = {}
    for name, category, subtype, address, city, county, zip5, lat, lon in prev.get("shops", []):
        if lat is not None and lon is not None:
            cache[geocode_key(address, city, zip5)] = (lat, lon)
    print(f"Loaded {len(cache)} previously-geocoded addresses from the existing data file.")
    return cache


def build_rollup(shops):
    rollup = {}
    for _, category, _, _, _, county, *_ in shops:
        bucket = rollup.setdefault(county, {code: 0 for code in CATEGORY_LABELS})
        bucket[category] += 1
    rollup_out = []
    for county, counts in sorted(rollup.items()):
        entry = {"county": county, "total": sum(counts.values())}
        entry.update(counts)
        rollup_out.append(entry)
    rollup_out.sort(key=lambda r: -r["total"])
    return rollup_out


def write_output(shops, unknown_types, excluded_school_count, in_progress, type_subtype_breakdown=None):
    geocoded_count = sum(1 for s in shops if s[7] is not None)
    top_breakdown = {}
    if type_subtype_breakdown:
        top_breakdown = dict(sorted(type_subtype_breakdown.items(), key=lambda kv: -kv[1])[:50])
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Texas Department of Licensing and Regulation (TDLR) via data.texas.gov (Socrata Open Data)",
        "source_url": f"https://data.texas.gov/Permits-and-Licensing/TDLR-All-Licenses/{RESOURCE_ID}",
        "geocoder": "U.S. Census Bureau batch geocoder (geocoding.geo.census.gov)",
        "is_sample": False,
        "geocoding_in_progress": in_progress,
        "categories": CATEGORY_LABELS,
        "excluded_school_or_instructor_records": excluded_school_count,
        "unclassified_license_types": unknown_types,
        "license_type_subtype_breakdown": top_breakdown,
        "geocode_coverage": {"geocoded": geocoded_count, "total": len(shops)},
        "shop_fields": ["name", "category", "subtype", "address", "city", "county", "zip", "lat", "lon"],
        "rollup": build_rollup(shops),
        "shops": shops,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(output, f, separators=(",", ":"))
    return geocoded_count


def git_checkpoint(message):
    """Commit + push whatever's currently in data/tx_shops.json. Assumes git
    user.name/user.email were already configured by the workflow. Safe to
    call even when there's nothing new to commit.

    Rebases onto the remote before pushing -- if someone edits another file
    in the repo (e.g. index.html) while this job is still running, a plain
    push would get rejected as non-fast-forward and this checkpoint (and
    everything after it, once the job eventually dies) would be lost."""
    try:
        subprocess.run(["git", "add", "data/tx_shops.json"], check=True)
        result = subprocess.run(["git", "commit", "-m", message], capture_output=True, text=True)
        if result.returncode != 0:
            if "nothing to commit" in (result.stdout + result.stderr).lower():
                return
            print(f"    git commit had nothing new or failed: {result.stdout}{result.stderr}", file=sys.stderr)
            return

        push = subprocess.run(["git", "push"], capture_output=True, text=True)
        if push.returncode != 0:
            print(f"    push rejected, rebasing onto remote before retrying: {push.stderr}", file=sys.stderr)
            subprocess.run(["git", "pull", "--rebase", "--autostash"], check=True)
            subprocess.run(["git", "push"], check=True)
        print(f"  checkpoint committed: {message}")
    except subprocess.CalledProcessError as e:
        print(f"    WARNING: checkpoint commit/push failed even after rebase: {e}", file=sys.stderr)


def geocode_chunk_once(rows_chunk):
    """Single attempt, no retry. rows_chunk: list of (row_index, street, city, state, zip)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for idx, street, city, state, zip5 in rows_chunk:
        writer.writerow([idx, street or "", city or "", state or "TX", zip5 or ""])

    files = {"addressFile": ("addresses.csv", buf.getvalue().encode("utf-8"), "text/csv")}
    data = {"benchmark": "Public_AR_Current", "returntype": "locations"}
    resp = requests.post(GEOCODER_URL, files=files, data=data, timeout=GEOCODE_REQUEST_TIMEOUT)
    resp.raise_for_status()

    out = {}
    for row in csv.reader(io.StringIO(resp.text)):
        if len(row) < 6:
            continue
        row_id, match, coords = row[0], row[2], row[5]
        if match == "Match" and coords:
            try:
                lon, lat = (float(x) for x in coords.split(","))
                out[row_id] = (lat, lon)
            except ValueError:
                pass
    return out


def geocode_chunk(rows_chunk):
    """Retries transient failures (502s, timeouts) with backoff. A real run
    against the full TX dataset showed these are common, not rare, so
    giving up after one attempt would silently lose a lot of coverage."""
    last_error = None
    for attempt in range(GEOCODE_MAX_RETRIES + 1):
        try:
            return geocode_chunk_once(rows_chunk)
        except requests.RequestException as e:
            last_error = e
            if attempt < GEOCODE_MAX_RETRIES:
                backoff = GEOCODE_RETRY_BACKOFF_SECONDS[min(attempt, len(GEOCODE_RETRY_BACKOFF_SECONDS) - 1)]
                print(f"    retry {attempt + 1}/{GEOCODE_MAX_RETRIES} after {type(e).__name__}, "
                      f"waiting {backoff}s...", file=sys.stderr)
                time.sleep(backoff)
    raise last_error


def main():
    geocode_cache = load_previous_geocode_cache()

    print("Looking up TDLR dataset field names...")
    fields = get_field_map()

    print("Fetching shop/salon/establishment license records (this covers all TDLR "
          "programs matched by keyword, then gets filtered down in Python)...")
    raw = fetch_all_records(fields)
    print(f"Total candidate records pulled: {len(raw)}")

    lt_f, sub_f, bn_f = fields["LICENSE TYPE"], fields["LICENSE SUBTYPE"], fields["BUSINESS NAME"]
    a1_f, a2_f, csz_f, county_f = (
        fields["BUSINESS ADDRESS-LINE1"], fields["BUSINESS ADDRESS-LINE2"],
        fields["BUSINESS CITY, STATE ZIP"], fields["BUSINESS COUNTY"],
    )

    shops = []          # [name, category, subtype, address, city, county, zip, lat, lon]
    unknown_types = {}  # literal license_type string -> count, for review
    excluded_school_count = 0
    geocode_inputs = []  # (row_index, street, city, state, zip)
    reused_from_cache = 0
    type_subtype_breakdown = {}  # (license_type, license_subtype) -> count, for every row seen

    for row in raw:
        license_type = row.get(lt_f)
        license_subtype = row.get(sub_f)
        breakdown_key = f"{license_type or ''} | {license_subtype or ''}"
        type_subtype_breakdown[breakdown_key] = type_subtype_breakdown.get(breakdown_key, 0) + 1

        category = classify(license_type)
        if category is None:
            excluded_school_count += 1
            continue
        if category == "UNKNOWN":
            unknown_types[license_type] = unknown_types.get(license_type, 0) + 1

        addr1 = row.get(a1_f) or ""
        addr2 = row.get(a2_f) or ""
        address = " ".join(filter(None, [addr1, addr2])).strip()
        city, state, zip5 = parse_csz(row.get(csz_f))
        county = (row.get(county_f) or "").strip().title()

        row_index = len(shops)
        shops.append([
            row.get(bn_f) or "",
            category,
            row.get(sub_f) or "",
            address,
            city or "",
            county or "Unknown",
            zip5 or "",
            None,  # lat, filled in below (from cache or fresh geocode)
            None,  # lon
        ])

        cached = geocode_cache.get(geocode_key(address, city, zip5)) if address and city else None
        if cached:
            shops[row_index][7], shops[row_index][8] = cached
            reused_from_cache += 1
        elif address and city:
            geocode_inputs.append((row_index, address, city, state or "TX", zip5))

    print(f"Classified {len(shops)} shop-level records "
          f"({excluded_school_count} school/instructor records excluded, "
          f"{sum(unknown_types.values())} unclassified, "
          f"{reused_from_cache} reused from a previous run's geocoding).")

    print("Real LICENSE TYPE | LICENSE SUBTYPE breakdown (top 25 by count) -- "
          "use this to sanity-check classify() against what TDLR actually publishes:")
    for key, count in sorted(type_subtype_breakdown.items(), key=lambda kv: -kv[1])[:25]:
        print(f"  {count:>7}  {key}")

    # Checkpoint #1: commit real shop counts/rollup right away, before the
    # slow part even starts. If everything after this fails, you still have
    # accurate totals instead of nothing.
    write_output(shops, unknown_types, excluded_school_count, in_progress=bool(geocode_inputs),
                 type_subtype_breakdown=type_subtype_breakdown)
    git_checkpoint(f"TX data: classified {len(shops)} shops, geocoding {len(geocode_inputs)} new addresses")

    if geocode_inputs:
        print(f"Geocoding {len(geocode_inputs)} new addresses via the Census Bureau batch geocoder "
              f"in batches of {GEOCODE_BATCH_SIZE} (this is the slow part)...")
        geocode_failures = 0
        batches_since_checkpoint = 0
        num_batches = (len(geocode_inputs) + GEOCODE_BATCH_SIZE - 1) // GEOCODE_BATCH_SIZE

        for batch_num, start in enumerate(range(0, len(geocode_inputs), GEOCODE_BATCH_SIZE), start=1):
            chunk = geocode_inputs[start:start + GEOCODE_BATCH_SIZE]
            try:
                results = geocode_chunk(chunk)
                for row_index, *_ in chunk:
                    match = results.get(str(row_index))
                    if match:
                        shops[row_index][7], shops[row_index][8] = match
            except requests.RequestException as e:
                print(f"    WARNING: geocode batch {batch_num}/{num_batches} failed: {e}", file=sys.stderr)
                geocode_failures += len(chunk)

            done = min(start + GEOCODE_BATCH_SIZE, len(geocode_inputs))
            print(f"  geocoded through {done} of {len(geocode_inputs)} (batch {batch_num}/{num_batches})...")

            batches_since_checkpoint += 1
            if batches_since_checkpoint >= CHECKPOINT_EVERY_N_BATCHES or batch_num == num_batches:
                still_in_progress = batch_num < num_batches
                write_output(shops, unknown_types, excluded_school_count, in_progress=still_in_progress,
                             type_subtype_breakdown=type_subtype_breakdown)
                git_checkpoint(f"TX data: geocoded {done} of {len(geocode_inputs)} new addresses "
                                f"(batch {batch_num}/{num_batches})")
                batches_since_checkpoint = 0

            time.sleep(GEOCODE_PAUSE_SECONDS)

        geocoded_count = sum(1 for s in shops if s[7] is not None)
        print(f"Geocoding complete: {geocoded_count} of {len(shops)} shops now have coordinates "
              f"({geocode_failures} lost to batch-request failures, the rest just didn't match).")
    else:
        print("Nothing new to geocode -- everything came from the cache or had no usable address.")

    print(f"Done. {len(shops)} shops across {len(build_rollup(shops))} counties.")


if __name__ == "__main__":
    main()
