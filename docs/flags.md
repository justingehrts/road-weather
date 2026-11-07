# Hazard flag rules

Thresholds below are **starting guesses to tune against Phase 5 verification**, not published values. Units °F; RST = road surface temperature.

| Flag | Trigger (starting point) | Notes | Verify against |
|---|---|---|---|
| Pavement frost | RST ≤ 32 and dewpoint > RST for ≥ 2 h; score = dewpoint minus RST summed over time | Raise the bar on clear, calm nights: 2 m dewpoint overstates moisture at the road | RWIS "frost" condition |
| Black ice / refreeze | Road wet (METRo liquid water > 0) and RST falls to ≤ 32 | Count crossings of 32; each one adds risk | RWIS "ice" / "black ice" |
| Flash freeze | Rain or wet road, then RST drops ≥ 6 in 3 h through 32 | Arctic fronts | RWIS condition change wet → ice |
| Wheel-track glazing | RST ≤ 20, snow rate light (< 0.04 in/h liquid) or blowing snow (wind ≥ 15 mph with fresh snow), heavy traffic | Little published guidance; tune hardest | RWIS ice/snow + crash or traffic-speed reports |
| Bridges first | Bridge site RST ≤ 32 while nearby road site RST > 32 | Run each overpass as METRo station type "bridge" | Bridge vs road RWIS pair |
| Salt struggles | RST ≤ 15 with any snow or ice on road | Rule-of-thumb threshold | Qualitative |
| Freezing rain | RRFS/HRRR freezing rain with RST ≤ 32 | RRFS precip type was revised in Oct 2026 for overforecasting freezing rain; check it yourself | RWIS ice + precip type |

## How `scripts/flags.py` turns each rule into code

Every rule is evaluated at each roadcast time step (METRo gives 20-minute steps). A flag is **active** at the steps where its condition holds; consecutive active steps are merged into one interval. All the numbers live in the `THRESHOLDS` dictionary at the top of `scripts/flags.py` and are copied into every output file, so you can see which settings produced a result.

The table leaves some things open. Here is what I chose. Each choice is a guess you can change:

| Flag | How the rule was made concrete |
|---|---|
| Pavement frost | A run of steps with RST ≤ 32 and dewpoint > RST, lasting ≥ 2 h, becomes one interval. `score` = sum of (dewpoint − RST in °F) × step length in hours (°F·h). "Clear, calm night" = HRRR total cloud cover < 25 % and wind < 5 mph. On those steps the dewpoint must exceed RST by **more than 3 °F** (`frost_clear_calm_margin_f`) to count. |
| Black ice / refreeze | "Wet" = METRo's liquid water on the road (`rain_mm`, METRo's `ra`) > 0. A crossing is a step where RST goes from > 32 to ≤ 32 and the road was wet on that step or the one before. The flag stays active from the crossing until RST rises above 32 again. `score` = how many crossings so far in the forecast (1, 2, 3…), since each one adds risk. |
| Flash freeze | At step *t*: RST 3 h earlier was > 32, RST now is ≤ 32, and the drop is ≥ 6 °F, and there was rain (METRo `qp-ra` > 0) or a wet road at some point in that 3 h window. `score` = size of the drop in °F. |
| Wheel-track glazing | Only evaluated for sites with `traffic: heavy` in `sites.yaml`. Needs RST ≤ 20 and either (a) light snow: METRo's hourly snowfall `qp-sn` > 0 but below 0.04 in/h liquid (at 10:1, 1 cm of snow = 1 mm of water), or (b) blowing snow: wind ≥ 15 mph and fresh snow, meaning snow fell in the last 6 h. |
| Bridges first | Only for `station_type: bridge` sites that name a `paired_road_site` in `sites.yaml`. The road site's RST is interpolated onto the bridge's time steps. |
| Salt struggles | RST ≤ 15 and METRo road condition is ice/snow (3), water-snow mix (4), frost (7) or icing rain (8). |
| Freezing rain | HRRR's freezing-rain flag (`cfrzr` ≥ 0.5, from the forcing CSV, nearest hour) with RST ≤ 32. Source is HRRR for now. When RRFS arrives, revisit this using the Oct 2026 precipitation-type change noted above. |

Flags that can't be evaluated (for example "bridges first" with no paired road site, or "wheel-track glazing" at a site not marked heavy traffic) are listed under `not_evaluated` with the reason, not silently dropped.

The "Verify against" column is for Phase 5 and is not implemented here.
