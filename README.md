# TX Shop Directory

A static web app showing licensed **barbering and cosmetology establishments**
in Texas: a searchable county-level rollup, a ranking chart, and a map of
individually geocoded shop locations.

Data comes from the Texas Department of Licensing and Regulation's
**"TDLR - All Licenses"** dataset on `data.texas.gov` (Socrata):
https://data.texas.gov/Permits-and-Licensing/TDLR-All-Licenses/7358-krk7

**No API key needed** for the license data. Addresses are geocoded using the
**free U.S. Census Bureau batch geocoder** (also no key), since — unlike
NY's registry — Texas doesn't publish coordinates directly.

The repo ships with **sample placeholder data** (`data/tx_shops.json`,
synthetic, clearly labeled) so the site works the moment you deploy it.

## Why this one's a bit different from the NY app

- **Texas consolidated its license types in 2024.** House Bill 1560 merged
  Barbershop, Beauty Shop, and Dual Shop licenses into a single **"Full-
  Service Establishment"** license. Barbershops got new license numbers
  immediately; beauty salons keep their old number but get the new label at
  their next renewal. So at any given moment, the live data is a **mix of
  legacy and current labels** — this app buckets both, and shows them as
  separate rows so you can see the transition happening over time.
- **No pre-published coordinates.** The fetch script geocodes every shop's
  address itself via the Census Bureau's batch geocoder, in batches of
  1,000. This is the main reason the TX fetch takes much longer than NY's —
  budget 20-60+ minutes for a full run, not seconds.
- **Classification is a best-effort keyword match**, because I couldn't
  verify every real `LICENSE TYPE` value TDLR actually uses before writing
  this. Anything the script doesn't recognize is **not silently dropped** —
  it's counted under "Unclassified" and listed in
  `unclassified_license_types` in the output JSON, and the app surfaces a
  banner if that bucket is non-empty. If you see that banner with real
  data, take a look at the listed type strings and let me know — the
  `classify()` function in `scripts/fetch_data.py` is where to fix it.

## 1. Run the data fetch

No signup required. Go to **Actions → Update TX shop data → Run workflow**.
This runs `scripts/fetch_data.py`, which:
1. Pulls all TDLR license records matching "shop," "salon," or
   "establishment" in the license type (filtering out unrelated trades TDLR
   also regulates, like electricians)
2. Classifies each into Barber / Salon / Full-Service-or-Dual /
   Unclassified, excluding schools and instructor licenses entirely
3. Geocodes every shop's address via the Census Bureau
4. Writes `data/tx_shops.json` and commits it

It's scheduled to re-run every Monday — adjust the `cron` line in
`.github/workflows/update-data.yml` if you want a different cadence, and
note the workflow has a 90-minute timeout to give the geocoding step room.

You can also run it locally:

```bash
pip install -r requirements.txt
python3 scripts/fetch_data.py
```

## 2. Turn on GitHub Pages

**Settings → Pages → Build and deployment → Source: Deploy from a branch →
Branch: `main`, folder: `/ (root)`**

## How it's put together

```
index.html          the page
css/style.css        styling (shares Boulevard brand tokens with the other two apps)
js/app.js             search, rollup table, ranking chart, and the Leaflet map
data/tx_shops.json    the dataset the page reads (static JSON, no server needed)
scripts/fetch_data.py pulls + classifies + geocodes fresh data
requirements.txt      just `requests`, needed for the geocoder's file upload
.github/workflows/    scheduled + on-demand data refresh
```

### Data notes

- "Shops" here means business-level establishment licenses, not individual
  practitioner licenses (Cosmetologist, Barber, Operator, Manicurist,
  Esthetician, etc.) and not schools — those are excluded from the count
  entirely, same principle as the NY app excluding Area Renters.
- Map coverage will be less than 100% — some addresses won't geocode
  cleanly (PO boxes, incomplete addresses, typos in the source data). The
  footer shows exactly what fraction mapped successfully. Ungeocoded shops
  still count in the totals and table; they just won't show a pin.
- County names come directly from TDLR's own `BUSINESS COUNTY` field, so
  they should be clean and consistent (no city-spelling-variant issue like
  NY's city-based rollup had).

## Customizing

- **Refine the category mapping:** edit `classify()` in
  `scripts/fetch_data.py`. If the `unclassified_license_types` banner shows
  up with real data, this is the first place to look.
- **Geocoding speed/batch size:** `GEOCODE_BATCH_SIZE` and
  `GEOCODE_PAUSE_SECONDS` in `scripts/fetch_data.py`.
- **Colors/fonts:** all in the `:root` block at the top of `css/style.css` —
  Boulevard's brand system, same as the other two apps.
