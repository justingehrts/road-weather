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

## Repo layout (planned)

- `sites.yaml`: the list of RWIS sites and their pavement layers (placeholders until real values are filled in).
- `.github/workflows/`: scheduled forecast job and any checks.
- `src/`: pipeline code (fetch observations, fetch forcing, run METRo, hazard flags, publish).
- `app/`: Streamlit app (reads from the `data` branch only).
- `data` branch: forecast output only; no source code.

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
