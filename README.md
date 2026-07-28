# NY Shop Directory

A static web app showing every **active licensed barbershop** and
**appearance enhancement business** (salons, nail, esthetics, waxing, hair
styling) in New York State: a searchable city-level rollup, a ranking chart,
and a full clustered map of individual shop locations.

Data comes straight from the New York State Department of State's public
registry, via the state's open data portal (`data.ny.gov`, built on
Socrata) — specifically the *"Active Appearance Enhancement and Barber
Business and Area Renter Licensees"* dataset:
https://data.ny.gov/Economic-Development/Active-Appearance-Enhancement-and-Barber-Business-/y3u4-jbgh

**No API key needed.** Unlike the Census-based tool, NY's open data API is
free and key-less for this volume of traffic.

The repo ships with **sample placeholder data** (`data/ny_shops.json`,
synthetic, clearly labeled) so the site works the moment you deploy it.
Follow the steps below to pull the real registry data.

## 1. Run the data fetch

No signup required this time. Go to **Actions → Update NY shop data → Run
workflow**. This runs `scripts/fetch_data.py`, which pages through the full
NY registry and overwrites `data/ny_shops.json` with real shop records
(name, address, city, category, lat/lon), then commits it.

It's also scheduled to re-run automatically every Monday, since this is a
live license registry rather than an annual survey — adjust the `cron` line
in `.github/workflows/update-data.yml` if you want a different cadence.

You can also run it locally:

```bash
python3 scripts/fetch_data.py
```

If you ever hit rate limits, get a free Socrata "app token" at
https://data.ny.gov/profile/edit/developer_settings and add it as a repo
secret named `NY_APP_TOKEN` — the workflow already looks for it, so no other
changes are needed.

## 2. Turn on GitHub Pages

**Settings → Pages → Build and deployment → Source: Deploy from a branch →
Branch: `main`, folder: `/ (root)`**

Your site will be live at `https://<your-username>.github.io/<repo-name>/`
within a minute or two.

## How it's put together

```
index.html          the page
css/style.css        styling (shares the Boulevard brand tokens with the Census app)
js/app.js             search, rollup table, ranking chart, and the Leaflet map
data/ny_shops.json    the dataset the page reads (static JSON, no server needed)
scripts/fetch_data.py pulls fresh data from data.ny.gov
.github/workflows/    scheduled + on-demand data refresh
```

The map uses [Leaflet](https://leafletjs.com/) with the
[marker clustering plugin](https://github.com/Leaflet/Leaflet.markercluster),
loaded from a CDN — both are free, open-source, and need no API key
(unlike Google Maps).

### Data notes

- **"Shops" = business-level licenses only** (`DOSAEBUSINESS` and
  `DOSBARSHOPOWNER` license types) — not individual practitioner licenses,
  and not Area Renters. Area Renters are independent contractors who rent a
  chair/space inside someone else's already-counted shop, so including them
  as separate "shops" would double-count locations. The fetch script tracks
  how many Area Renter (and any other) records it excluded — see
  `excluded_area_renter_records` in the output JSON.
- NY's registry doesn't split appearance enhancement businesses into
  "beauty salon" vs. "nail salon" vs. "esthetics" the way Census NAICS
  codes do — a single Appearance Enhancement Business license can cover any
  combination of cosmetology, esthetics, nail specialty, natural hair
  styling, and waxing services. If you want a finer breakdown, the separate
  *individual practitioner* license dataset
  (https://data.ny.gov/Economic-Development/Active-Appearance-Enhancement-and-Barber-Individua/ucu3-8265)
  does break down by discipline, but it's licensee counts, not shop counts.
- City rollups are based on the `business_city` field as entered by each
  licensee, so minor spelling/casing variants (e.g., "New York" vs "NY,
  NY") could split a city into more than one row. Worth spot-checking if a
  city's numbers look surprisingly low.

## Customizing

- **Add individual practitioner data:** point a second fetch at the
  `ucu3-8265` resource and merge it in.
- **Change map defaults:** initial center/zoom is set in `buildMap()` in
  `js/app.js`.
- **Colors/fonts:** all in the `:root` block at the top of `css/style.css` —
  currently Boulevard's brand system, same as the Census app.
