# How forecasts are verified

`scripts/verify.py` runs once a day (`.github/workflows/verify.yml`) and writes `data/verification/summary.json` to the `data` branch. The Verification page of the app reads that file. It re-checks the last 30 days every time, so it is safe to re-run.

## Temperature: bias and RMSE by lead time

For every past roadcast (one per site per HRRR cycle) and every RWIS observation that falls inside that roadcast's period:

- The roadcast is interpolated to the observation time (METRo gives 20-minute steps, stations report about hourly).
- **Error = forecast minus observed**, in °F, for road surface temperature and for air temperature.
- Errors are grouped by **site** and by **lead time**: hours from the HRRR cycle time (the 12Z run is hour 0 at 12:00 UTC) in 6-hour bins, `0-6` to `42-48`. The road forecast itself starts a few hours after the cycle, so the first bin is thin.
- **Bias** = average error (above 0: forecast too warm). **RMSE** = square root of the average squared error (typical size of the error). **MAE** = average absolute error.

Only observations from the roadcast start onward are used, so the observations METRo was initialised from are never used to grade it.

Each observation is compared with every roadcast that covered it (about 8 per observation with four cycles a day and 48-hour forecasts). That is normal for forecast checking, but it means results from consecutive cycles are not independent.

## Hazard flags: hit rate and false alarms

Each flag in `data/flags/<cycle>.json` is checked at each observation time inside the roadcast period. A flag counts as **on** if the observation time is inside one of its active intervals. The **observed hazard** comes from the RWIS surface condition (or observed road temperatures for bridges):

| Flag | Hazard counts as observed when... |
|---|---|
| Pavement frost | the station reports **frost** |
| Black ice / refreeze | the station reports **ice** or black ice |
| Flash freeze | the station reports ice and reported **wet** within the previous 3 hours |
| Wheel-track glazing | the station reports **ice or snow** (crash and traffic-speed data are not available) |
| Bridges first | the bridge's observed road temperature is at or below 32°F while its `paired_road_site` is above 32°F |
| Freezing rain | the station reports **ice** (precipitation type is not observed) |
| Salt struggles | not scored: a qualitative rule with nothing to measure it against |

Each observation falls into one of four boxes:

| | Hazard observed | Not observed |
|---|---|---|
| **Flag on** | hit | false alarm |
| **Flag off** | miss | correct negative |

Three numbers come from the boxes. The page shows all three, because "false-alarm rate" is used for two different things:

- **Hit rate** = hits / (hits + misses). Of the times the hazard happened, how often was it flagged?
- **False-alarm ratio** = false alarms / (hits + false alarms). Of the times the flag was on, how often did nothing happen?
- **False-alarm rate** = false alarms / (false alarms + correct negatives). Of the times nothing happened, how often was the flag on?

A number shows as "–" when its bottom line is zero (for example, no hazard was ever observed). Flags with fewer than 10 observed hazard times are marked with `*`: the rates are not reliable yet.

### Surface condition words

The station's surface condition is plain text. OHGO reports it in the pavement sensor's `Status` field (its documentation only calls it the "status of the sensor" and lists no values). Seen so far: `Dry`; `Unknown` marks an empty sensor slot and is treated as no information. `classify_condition()` in `scripts/verify.py` maps words to categories: `frost`, `ice` (also "black ice"), `snow` (also "slush"), `wet` (also "damp", "moist", "chemically wet") and `dry`. "Watch" states such as "Ice Watch" mean ice is possible, not present, so they do not count as ice or snow. If OHGO uses other words, edit that function.

Observations with **no** surface condition are skipped, not counted as "no hazard". If the station never reports one, the page says so and only temperature (and "Bridges first") is scored.
