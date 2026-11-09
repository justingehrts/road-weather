# CLAUDE.md

Automated road surface temperature forecasts for RWIS (Road Weather Information System) sites near Columbus, Ohio.

## Pipeline

```
OHGO RWIS observations ─┐
                        ├─> METRo road weather model -> hazard flags -> `data` branch -> Streamlit app
HRRR forcing ───────────┘
(later: RRFS ensemble)
```

1. **Observations**: pull current RWIS readings (air/road temp, etc.) from the OHGO API.
2. **Forcing**: HRRR model output for each site (later replaced/augmented by RRFS ensemble members).
3. **Model**: run METRo (road weather model) per site using the site's pavement layers from `sites.yaml`.
4. **Hazard flags**: derive flags (e.g. freezing road surface) from METRo output.
5. **Publish**: write results to the `data` branch.
6. **Display**: the Streamlit app reads from the `data` branch.

## METRo notes

- METRo 4.0.1 (pinned commit) is built by `scripts/build_metro.sh`, with a small patch for Python 3.12 / numpy 1.24+ (`scripts/metro_patches/`). The build is cached in Actions.
- METRo runs on the runner's **system Python** (apt `python3-libxml2`), *not* the project's 3.11. `scripts/run_metro.py` runs on 3.11 and launches METRo as a separate process. Don't `pip install` METRo's dependencies into the 3.11 environment.
- Pavement layer materials in `sites.yaml` must be one of METRo's four: asphalt, cement/concrete, crushed rock/gravel, sand.
- METRo needs a few hours of observations (>=2 road-temperature readings, one within 4 h with a subsurface reading). Sites without enough are skipped with a message.
- Observations are collected **hourly** by `.github/workflows/fetch-obs.yml` (OHGO only returns the latest reading, so history has to be built up on the `data` branch). **The hourly schedule is currently paused** (commented out in that file) because there is no OHGO API key yet; re-enable it per the comment at the top of the workflow. Until then METRo has no observations and skips every site; a forecast run where every site is skipped for lack of data finishes green with a yellow warning (only real errors turn it red).
- The forecast workflow (`forecast.yml`) runs at 02, 08, 14 and 20 UTC (about 2 h after each HRRR cycle). GitHub often starts scheduled runs late (sometimes by hours), so `fetch_hrrr.py` targets the newest 00/06/12/18Z cycle that started at least 2 h ago (`MIN_CYCLE_AGE`); a younger cycle cannot have forecast hour 48 yet. It then waits up to 60 min for forecast hour 48. If no complete HRRR run is found, METRo is skipped.
- All the workflows push to `data`, so each retries with a rebase if the push is rejected. Each owns its own files: the hourly one writes `data/obs`; the forecast one `data/forcing`, `data/roadcast`, `data/flags` and `data/latest.json`; the daily verify one `data/verification`.
- Flag scoring needs OHGO to report a surface condition (dry/wet/frost/ice...). Its public docs do not list that field, so `fetch_rwis.py` may store it blank; the Verification page says so when that happens.

## Streamlit app notes

- Deploy it from `main` with entrypoint `app/streamlit_app.py` (Streamlit Community Cloud is free); set Python to 3.11 in the deploy settings.
- The Forecast page finds the newest forecast through `data/latest.json`, then loads `data/flags/<cycle>.json`, `data/roadcast/<cycle>_<site>.csv` and `data/obs/<site>.csv` from `raw.githubusercontent.com/justingehrts/road-weather/data/`. The Verification page reads `data/verification/summary.json`. Set `ROAD_WEATHER_DATA_URL` to point the app somewhere else for testing.
- Data stays in UTC; the app converts to Eastern only for display. Charts follow the data-viz palette rules (one temperature axis, validated colors, table view).

## Hard rules

- **Everything runs on GitHub Actions.** The repo is public and uses free runners. No paid services, no paid APIs, no self-hosted infrastructure.
- **Never commit forecast output to `main`.** `main` is the live Streamlit branch; every commit there redeploys the app. Forecast/results files go only to the `data` branch.
- **Secrets only via GitHub Actions secrets.** The only secret so far is `OHGO_API_KEY`. Never hard-code keys, never write them to files, logs, or commits, and never print them in workflow output.
- **Python 3.11.** Use it in workflows (`actions/setup-python` with `3.11`) and keep code compatible with it.
- **Keep it free-tier friendly.** Watch runner time and artifact/storage size; prefer small outputs (e.g. compact CSV/JSON/Parquet) and prune old data on `data`.

## Working with the user (novice)

The owner is new to this. In **every PR description**, include a plain-language section that says:

- What changed and why, in everyday words (define jargon like RWIS, HRRR, METRo the first time).
- What the owner needs to do (if anything), e.g. add a secret, merge, enable a workflow.
- What could go wrong and how to undo it.

Prefer small, focused PRs. Don't open a PR unless asked. Don't make sweeping changes without explaining them.

## Repo layout

- `sites.yaml`: the list of RWIS sites, pavement layers, bridge pairings and traffic level (placeholders until real values are filled in).
- `scripts/`: pipeline code. `fetch_rwis.py`, `fetch_hrrr.py`, `build_metro.sh`, `run_metro.py`, `flags.py`, `verify.py`.
- `docs/flags.md`: the hazard-flag rules (source of truth for `scripts/flags.py`; edit thresholds in `THRESHOLDS` at the top of that script).
- `docs/verification.md`: how forecasts and flags are checked against RWIS observations (`scripts/verify.py`).
- `app/`: Streamlit app: `streamlit_app.py` (entry point + navigation), pages `forecast.py` and `verification.py`, plus `charts.py`, `road_data.py`, `ui.py`, `requirements.txt`. It only **reads** from the `data` branch via raw GitHub URLs.
- `tests/`: pytest tests (`python -m pytest tests -q`). Run by `.github/workflows/tests.yml` on every PR.
- `.github/workflows/`: `fetch-obs.yml` (hourly), `forecast.yml` (4x/day), `verify.yml` (daily), `tests.yml`, and `list-ohgo-sites.yml` (manual helper that lists OHGO site IDs near Columbus via `scripts/list_ohgo_sites.py`).
- `data` branch: pipeline output only; no source code. `data/obs/`, `data/forcing/`, `data/roadcast/`, `data/flags/<cycle>.json`, `data/latest.json` (pointer to the newest cycle that the app reads), and `data/verification/summary.json` (daily).

## Site configuration

`sites.yaml` holds one entry per site:

| Field | Meaning |
|---|---|
| `id` | Short stable identifier used in file names and output |
| `name` | Human-readable name |
| `lat`, `lon` | Decimal degrees (WGS84) |
| `ohgo_site_id` | The site's ID in the OHGO RWIS API |
| `station_type` | `road` or `bridge` (bridges lose heat from below and freeze sooner, so METRo needs to know) |
| `pavement_layers` | Ordered list, top to bottom, each with `material` and `thickness_m` |

Current entries are **placeholders**; real IDs, coordinates and layer data must be confirmed before trusting any forecast.

## Conventions

- Read site data from `sites.yaml`; never hard-code site details in code.
- Fail loudly on missing/invalid observations or forcing, and skip a site rather than publish a bad forecast.
- Keep timestamps in UTC internally; convert to Eastern time only for display in the app.
- Add or update tests/validation (e.g. a check that `sites.yaml` parses and every site has required fields) when changing config or pipeline code.
