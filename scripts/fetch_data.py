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
GEOCODE_BATCH_SIZE = 1000
GEOCODE_PAUSE_SECONDS = 1.0

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


def geocode_chunk(rows_chunk):
    """rows_chunk: list of (row_index, street, city, state, zip). Returns {row_index: (lat, lon)}."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for idx, street, city, state, zip5 in rows_chunk:
        writer.writerow([idx, street or "", city or "", state or "TX", zip5 or ""])

    files = {"addressFile": ("addresses.csv", buf.getvalue().encode("utf-8"), "text/csv")}
    data = {"benchmark": "Public_AR_Current", "returntype": "locations"}
    resp = requests.post(GEOCODER_URL, files=files, data=data, timeout=180)
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


def main():
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

    for row in raw:
        license_type = row.get(lt_f)
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
            None,  # lat, filled in after geocoding
            None,  # lon
        ])
        if address and city:
            geocode_inputs.append((row_index, address, city, state or "TX", zip5))

    print(f"Classified {len(shops)} shop-level records "
          f"({excluded_school_count} school/instructor records excluded, "
          f"{sum(unknown_types.values())} unclassified -- see unclassified_license_types in output).")

    print(f"Geocoding {len(geocode_inputs)} addresses via the Census Bureau batch geocoder "
          f"in batches of {GEOCODE_BATCH_SIZE} (this is the slow part)...")
    geocode_failures = 0
    for start in range(0, len(geocode_inputs), GEOCODE_BATCH_SIZE):
        chunk = geocode_inputs[start:start + GEOCODE_BATCH_SIZE]
        try:
            results = geocode_chunk(chunk)
        except requests.RequestException as e:
            print(f"    WARNING: geocode batch at offset {start} failed: {e}", file=sys.stderr)
            geocode_failures += len(chunk)
            time.sleep(GEOCODE_PAUSE_SECONDS)
            continue
        for row_index, *_ in chunk:
            match = results.get(str(row_index))
            if match:
                shops[row_index][7], shops[row_index][8] = match
        print(f"  geocoded through {min(start + GEOCODE_BATCH_SIZE, len(geocode_inputs))} of {len(geocode_inputs)}...")
        time.sleep(GEOCODE_PAUSE_SECONDS)

    geocoded_count = sum(1 for s in shops if s[7] is not None)
    print(f"Geocoded {geocoded_count} of {len(shops)} shops "
          f"({geocode_failures} lost to batch-request failures, the rest just didn't match).")

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

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "Texas Department of Licensing and Regulation (TDLR) via data.texas.gov (Socrata Open Data)",
        "source_url": f"https://data.texas.gov/Permits-and-Licensing/TDLR-All-Licenses/{RESOURCE_ID}",
        "geocoder": "U.S. Census Bureau batch geocoder (geocoding.geo.census.gov)",
        "is_sample": False,
        "categories": CATEGORY_LABELS,
        "excluded_school_or_instructor_records": excluded_school_count,
        "unclassified_license_types": unknown_types,
        "geocode_coverage": {"geocoded": geocoded_count, "total": len(shops)},
        "shop_fields": ["name", "category", "subtype", "address", "city", "county", "zip", "lat", "lon"],
        "rollup": rollup_out,
        "shops": shops,
    }

    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "tx_shops.json")
    with open(out_path, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    print(f"Wrote {out_path}: {len(shops)} shops across {len(rollup_out)} counties.")


if __name__ == "__main__":
    main()
