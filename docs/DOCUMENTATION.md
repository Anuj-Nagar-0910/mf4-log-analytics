# MF4 Log Analytics: Python documentation

Reference for `analyze_mf4_startup_order.py` (analysis engine + CLI) and `mf4_startup_order_gui/` (local dashboard).
For a visual walk-through open [workflow.html](workflow.html) (interactive diagram).

Contents: [1 Concepts](#1-concepts) · [2 Install and run](#2-install-and-run) · [3 Signal map](#3-signal-map) ·
[4 Pipeline step by step](#4-pipeline-step-by-step) · [5 Decision rules](#5-decision-rules) ·
[6 Output columns](#6-output-columns) · [7 Module reference](#7-module-reference) · [8 Dashboard](#8-dashboard) ·
[9 Extending and limits](#9-extending-and-limits)

---

## 1. Concepts

The target is a controller with **two processing paths** (PATH1 = master "PA", PATH2 = slave "PB"). Each path is recorded
as its own channel group, with its **own time base**. A recording can contain many **power-ups**, and in each power-up a
path may be reset several times before it is released and its application starts.

For every power-up the tool answers:

| Question | Where it is answered |
|---|---|
| When did each path start its application, and which was first? | raw times, `corrected_diff_*`, `offsetfree_diff_ms`, `Order_Verdict` |
| How far apart are the two clocks? | `clock_offset_ms`, `ipc_latency_ms`, `Offset_Flag` |
| How many resets did each path do before release? | `resets_p1`, `resets_p2`, `scenario` (`PA1-PB0`, `PA3-PB3`, ...) |
| Why was each path released? | `p1_release_reason`, `p2_release_reason` |
| Did the system state show value 2 afterwards? | `p1_stsys2`, `p2_stsys2`, `*_stsys_sequence` |
| Is the start-up order requirement met? | `order_rule`, `Order_Result` (`OK` / `NOK` / `CHECK` / `INFO`) |

**Sign convention everywhere: `diff = PATH2 - PATH1`.** A positive value means PB started after PA.

**Why two measurements.** Raw timestamps mix the real start difference with the clock offset. The tool measures the
start difference twice, independently, and only reports a violation when both agree:

1. *Offset-corrected*: raw difference minus the clock offset estimated from IPC alive-counter pairs.
2. *Offset-free*: each path's own-clock delay between sending its first "application running" frame and receiving the
   sibling's first one. The clock offset cancels out of `(da - db) / 2`.

---

## 2. Install and run

```bash
pip install -r requirements.txt        # asammdf, numpy, pandas  (developed and tested on Python 3.11)
```

### CLI

```bash
python analyze_mf4_startup_order.py [target] [options]
```

| Argument | Default | Meaning |
|---|---|---|
| `target` | `Analysis_file_1.mf4` next to the script | one `.mf4` file, or a folder (batch mode, all `*.mf4` / `*.MF4`) |
| `--output` | `<mf4 name>_Startup_Order.csv` (file) or `Startup_Order_Analysis.csv` (folder), next to the script | output CSV |
| `--window-ms` | `60` | search window after release for the first application counter update |
| `--stsys-window-ms` | `200` | window after release in which "system state == 2" is checked |
| `--same-frame-tol-ms` | `0.2` | `abs(PB - PA)` up to this is `SAME_FRAME` (one 0.2 ms sample) |
| `--offset-suspect-ms` | `1.0` | `abs(clock offset)` above this is flagged `OFFSET_SUSPECT` |
| `--signals` | none | JSON signal map, see [section 3](#3-signal-map) |

The run prints a summary (counts per scenario, release reason, verdict, result, offset range) and writes one CSV row per
power-up.

### Try it without real data

```bash
python demo/make_demo_mf4.py                     # writes demo/demo_startups.mf4 (7 power-ups, all verdict types)
python analyze_mf4_startup_order.py demo/demo_startups.mf4
```

### As a library

```python
from types import SimpleNamespace
import analyze_mf4_startup_order as core

args = SimpleNamespace(window_ms=60.0, stsys_window_ms=200.0, same_frame_tol_ms=0.2, offset_suspect_ms=1.0)
rows = core.analyze_file("recording.mf4", args)          # -> list[StartupRow]
import pandas as pd
from dataclasses import asdict
df = pd.DataFrame([asdict(r) for r in rows])
```

Pass a dict as third argument (`analyze_file(path, args, timelines={})`) to also collect per-start-up event lists for
drawing (used by the dashboard).

---

## 3. Signal map

Channel names are **not** hard-coded. The module-level dict `SIG` maps a logical role to the channel name in your
recording. Later sources override earlier ones:

1. `DEFAULT_SIGNALS` in the script (placeholder names)
2. `config/signals.local.json` (git-ignored, put your real names here)
3. the file named by environment variable `MF4_SIGNALS_FILE`
4. the file given with `--signals`

Keys starting with `_` (for example `_comment`) are ignored. Template: [`config/signals.example.json`](../config/signals.example.json).

| Key | Role | Needed |
|---|---|---|
| `path1_marker`, `path2_marker` | substring (case-insensitive) of the channel group's acquisition **source path** that identifies each path | yes |
| `init_done` | flag, `1` once the path finished init (= release). `1 -> 0` marks a power-up | **required** |
| `error_counter` | reset counter, steps `1, 2, 3` per reset, `4` = maximum resets reached | **required** |
| `main_cnt` | counter that starts counting when the application runs; its first non-zero update is the "application start" | **required** |
| `aux_cnt` | second application counter (second raw start time) | optional |
| `hold_timeout` | marker set when the reset-hold timeout expired | optional |
| `hold_cnt` | reset-hold counter value at release | optional |
| `sync_debounce` | PATH1 sync debounce counter (25 ticks = 25 ms) | optional |
| `complete_latch` | PATH2 latch set by the master's RESET_COMPLETE message | optional |
| `sys_state` | system-state signal broadcast by the controller; value 2 is checked after release | optional |
| `ipc_tx`, `ipc_rx` | IPC alive-counter **arrays**; element `N` is read from channel `<name>._N_` (or from a structured channel with that field) | optional, but needed for clock offset |
| `ipc_mov_a` (0), `ipc_trq_a` (1), `ipc_mov_b` (9), `ipc_trq_b` (10), `ipc_complete_a` (18) | indices into those arrays: application-running frame PATH1->PATH2, cyclic 1 ms reference frame PATH1->PATH2, same for PATH2->PATH1, and the RESET_COMPLETE frame | optional |

A file is **skipped** (with a message) when `init_done`, `error_counter` or `main_cnt` is missing on either path, or when
both path groups cannot be found. Missing optional channels print `[warn]` and the dependent columns become NaN.

---

## 4. Pipeline step by step

Everything below happens inside `analyze_file()` for one recording. All times are in seconds on the path's own clock
unless noted; CSV columns ending in `_ms` are milliseconds.

### Step 1: Load and discover paths

`MDF(file)` opens the file lazily. `find_path_groups()` walks `mdf.groups` and picks the group that contains both
`error_counter` and `main_cnt`; the source path of the `error_counter` channel must contain the PATH1 or PATH2 marker. `PathSignals`
wraps one group and loads channels on demand (`get()`, `ipc()`), cached per channel, read raw as `int64`.

### Step 2: Segment into power-ups

`segment_boundaries()` takes the falling edges (`1 -> 0`) of `init_done` on both paths, sorts them, and merges edges closer
than `BOUNDARY_MERGE_S` (1 s) into one power-up. If the first power-up starts more than 1 s after the recording began,
the recording start is added as a boundary. Segment `k` runs from boundary `k` to boundary `k+1` (or the end).

### Step 3: Per power-up, per path: events

- **Release** = first rising edge (`0 -> non-zero`) of `init_done` inside the segment (`rising_edges`).
- **Resets** = steps of `error_counter` into 1, 2 or 3 between segment start and release + 2 ms (`count_resets`). A step
  into 4 sets `max_resets_pX = 'Y'`. `scenario = f"PA{n1}-PB{n2}"`.
- A segment with no release and no resets on both paths is dropped (recording started or ended mid-run).
- **Raw application start** = timestamp of the first change to a **non-zero** value of `main_cnt` (and `aux_cnt`) within
  `[release - 5 ms, release + window]` (`first_change`). Counters are re-initialised to 0 at power-up, which is not an event.
- **Sample period** = median time step of `main_cnt` around the release; it is the resolution of every difference.
- **Latent start** (`pX_mor_running_before_release`): `Y` if `main_cnt` already changed between the end of the last
  reboot gap (`last_reboot_end`, a data gap above `REBOOT_GAP_S` = 20 ms) and 5 ms before release.

### Step 4: Clock offset from IPC counter pairs

`ipc_lag()` matches each change of the sender's alive counter with the receiver's change to the **same value** within
+-50 ms (`OFFSET_PAIR_MATCH_S`) and returns send time and `receive time - send time`. It does this for the 1 ms
reference frames in both directions, in the window `[ref + 5 ms, ref + 105 ms]` where `ref` is the later release
(widened to 300 ms if fewer than `MIN_OFFSET_PAIRS` = 5 pairs in either direction).

`lag_at()` evaluates each direction's lag at the PATH1 application start: a linear fit (`np.polyfit`) if there are at
least 5 pairs spanning more than 10 ms (the offset can drift by several ms/s right after power-up), otherwise the median.

```
lab = lag PATH1 -> PATH2  =  latency + offset
lba = lag PATH2 -> PATH1  =  latency - offset
clock_offset_ms = (lab - lba) / 2          ipc_latency_ms = (lab + lba) / 2
corrected_diff  = raw_diff - clock_offset
```

### Step 5: Offset-free cross-check

Needs both releases. Per path: `own_tx` = first non-zero change of its own "application running" TX counter; `sibling_rx` =
first non-zero change of the sibling's RX counter after the path's last reboot. In each path's own clock:

```
da = p1_rx_sibling - p1_own_tx      =  (B start - A start) + latency
db = p2_rx_sibling - p2_own_tx      =  (A start - B start) + latency
offsetfree_diff_ms    = (da - db) / 2     # B start - A start, clock offset cancels
offsetfree_latency_ms = (da + db) / 2
```

### Step 6: Handshake and release reasons

`complete_tx_p1_time` / `complete_rx_p2_time` are the first RESET_COMPLETE TX (PATH1) and RX (PATH2) after each path's
last reboot. `release_reason()` checks in this order and returns the first match:

| # | Reason | Condition |
|---|---|---|
| 0 | `NO_RELEASE` | no release found |
| 1 | `MAX_RESETS` | `error_counter` reached 4 before release |
| 2 | `NO_WAIT` | 2 or more resets before release (path does not wait for its sibling) |
| 3 | `TIMEOUT_300MS` | `hold_timeout` marker is 1 at release |
| 4a | `MASTER_COMPLETE` | PATH2 only: RESET_COMPLETE received within `[-2 ms, +1 ms]` of release, or the latch is 1 |
| 4b | `DEBOUNCE_25MS` | PATH1 only: `sync_debounce` reached 25 within 5 ms before release |
| 5 | `OTHER` | none of the above |

### Step 7: System state

`stsys2_duration()` reports `Y` / `N` / `NA` and the duration (ms) of `sys_state == 2` in `[release, release + window]`.
`stsys_sequence()` renders the value sequence such as `6>2(4.0ms)>0` (every segment except the last, which is
open-ended, carries its duration).

### Step 8: Verdict and result

See [section 5](#5-decision-rules). Then one `StartupRow` is appended; with `timelines` given, `collect_timeline()` stores
resets, reboot gaps, debounce start, timeout marker and system-state segments for the dashboard.

---

## 5. Decision rules

`classify(d, tol)` -> `NA` if NaN, `PA_FIRST` if `d > tol`, `PB_BEFORE_PA` if `d < -tol`, else `SAME_FRAME`.

```
corrected_verdict  = classify(corrected_diff_mor_ms)
offsetfree_verdict = classify(offsetfree_diff_ms)    only if  0 <= latency <= 2 ms  and  abs(diff) <= 50 ms, else NA
```

| corrected (cv) | offset-free (fv) | `Order_Verdict` |
|---|---|---|
| NA | NA | `INCONCLUSIVE` |
| NA | any | fv |
| any | NA, or equal to cv | cv |
| differ, one is `PB_BEFORE_PA` | | `INCONCLUSIVE` (a violation needs both methods to agree) |
| differ, `Offset_Flag == OFFSET_SUSPECT` | | fv (clock independent) |
| differ otherwise | | cv |

`Offset_Flag`: `NO_OFFSET` if no offset could be computed, `OFFSET_SUSPECT` if `abs(offset) > --offset-suspect-ms` or the
latency is outside 0..2 ms, else `OK`.

`order_rule` is `ENFORCED` when PATH2's release reason is `MASTER_COMPLETE` (PB was waiting for the master), otherwise
`NOT_APPLICABLE` with a reason (`PB_TIMEOUT_300MS`, `PB_NO_WAIT`, `PB_MAX_RESETS`, `NO_RELEASE`, ...).

| `order_rule` | `Order_Verdict` | `Order_Result` |
|---|---|---|
| not enforced | any | `INFO` |
| enforced | `PB_BEFORE_PA` | `NOK` |
| enforced | `INCONCLUSIVE` | `CHECK` |
| enforced | otherwise | `OK` |

Legacy columns kept for comparison with older scripts: `Flag` = `NOK` if `abs(raw diff) > 5 ms`; `Negative_Flag` =
`NEGATIVE` if the raw diff is below 0.

---

## 6. Output columns

One row per power-up, 60 columns (`dataclass StartupRow`, same order in CLI and GUI exports).

| Group | Columns |
|---|---|
| Identity | `file_name`, `startup_idx`, `segment_start`, `sample_period_ms` |
| Scenario | `scenario`, `reset_paths` (`NONE`/`P1`/`P2`/`BOTH`), `trigger_path`, `resets_p1/p2`, `max_resets_p1/p2` |
| Reset / release times | `error_counter_time`, `p1/p2_first_error_time`, `p1/p2_release_time`, `p1/p2_release_reason`, `p1_sync_debounce_at_release`, `p1/p2_hold_cnt_at_release` |
| Raw start times | `p1/p2_mov_start_time`, `path1/path2_mor_inc_time`, `time_difference_sec`, `raw_diff_mor_ms`, `raw_diff_mov_ms` |
| Clock offset | `clock_offset_ms`, `ipc_latency_ms`, `offset_pairs_n`, `corrected_diff_mor_ms`, `corrected_diff_mov_ms` |
| Offset-free | `p1/p2_own_mov_tx_time`, `p1/p2_rx_sibling_mov_time`, `p1/p2_ownclock_rx_delay_ms`, `offsetfree_diff_ms`, `offsetfree_latency_ms` |
| Handshake | `complete_tx_p1_time`, `complete_rx_p2_time`, `p1/p2_mor_running_before_release` |
| System state | `p1/p2_stsys2`, `p1/p2_stsys2_ms`, `p1/p2_stsys_sequence` |
| Verdicts | `corrected_verdict`, `offsetfree_verdict`, `Order_Verdict`, `order_rule`, `order_rule_reason`, `Order_Result`, `Offset_Flag`, `Flag`, `Negative_Flag` |

Times without a unit suffix are seconds on the path's own clock.

---

## 7. Module reference

`analyze_mf4_startup_order.py`

| Name | Purpose |
|---|---|
| `DEFAULT_SIGNALS`, `SIG`, `load_signals(path)` | signal map and its loader ([section 3](#3-signal-map)) |
| `PathSignals(mdf, group, label)` | lazy, cached channel access for one path; `get(name)`, `ipc(direction, index)` |
| `edge_indices(v)` | indices where a sampled signal changes |
| `first_change(sig, a, b)` | time of the first change to a non-zero value in `[a, b]` |
| `value_at(sig, x)` | sample-and-hold value at time `x` |
| `rising_edges(sig, a, b)`, `falling_edges(sig)` | `0 -> non-zero` / `non-zero -> 0` transitions |
| `last_reboot_end(sig, a, b)` | end of the last data gap above 20 ms (reset / power-up) |
| `ipc_lag(tx, rx, a, b)` | `(send times, receive - send)` for equal alive-counter values |
| `lag_at(times, lags, t)` | lag at time `t` (linear fit or median) |
| `stsys2_duration`, `stsys_segments`, `stsys_sequence` | system-state analysis |
| `find_path_groups(mdf)` | group indices of PATH1 and PATH2 |
| `segment_boundaries(p1, p2)` | power-up start times |
| `count_resets(sig, a, b)` | `(resets, reached_max 'Y'/'N', first reset time)` |
| `release_reason(path, rel, seg_start, is_p2, complete_rx)` | reason string, see step 6 |
| `classify(d, tol)` | `PA_FIRST` / `SAME_FRAME` / `PB_BEFORE_PA` / `NA` |
| `collect_timeline(...)` | event lists for the dashboard drawing (not in the CSV) |
| `StartupRow` | dataclass of one CSV row |
| `analyze_file(path, args, timelines=None)` | whole pipeline for one file, returns `list[StartupRow]` |
| `main()` | CLI |

Tuning constants (top of the file): `SYNC_DEBOUNCE_TICKS = 25`, `REBOOT_GAP_S = 0.020`, `BOUNDARY_MERGE_S = 1.0`,
`OFFSET_PAIR_MATCH_S = 0.050`, `MIN_OFFSET_PAIRS = 5`.

`demo/make_demo_mf4.py` builds the synthetic recording: `Scenario` dataclass list, `build(path)`; all times are integer
sample indices (0.2 ms) so the expected result of every scenario is exact.

---

## 8. Dashboard

```bash
python mf4_startup_order_gui/server.py [--port 8766] [--no-browser]     # or run_gui.bat on Windows
```

Binds to `127.0.0.1` only; tries ports `8766..8776`. It imports `analyze_file()` from the CLI module, so numbers are
identical. Analyses run one at a time (a lock, because `analyze_file` prints to stdout, which is captured per file).

| Environment variable | Effect |
|---|---|
| `MF4_GUI_START` | folder the file browser opens in |
| `MF4_GUI_AUTHOR` | owner name shown in the UI and reports (default: OS user name) |
| `MF4_GUI_CACHE_MAX_DAYS` | max age of cached results in days, `0` = unlimited |
| `MF4_SIGNALS_FILE` | signal map used by the analysis |

**Result cache**: `%LOCALAPPDATA%\mf4_startup_order_cache\<sha1>.json`. The key combines the absolute path, file size and
modification time, the settings, the signal map and a hash of the analysis script, so any change of file, settings,
signal map or algorithm triggers a fresh analysis.

**HTTP API**

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | the single-page UI (`index.html`) |
| GET | `/api/roots`, `/api/browse?path=` | file browser |
| GET | `/api/job?id=` | job state, file statuses, rows and timelines |
| GET | `/api/cache` | cache statistics |
| POST | `/api/analyze` | body `{paths, settings, use_cache, cache_max_days}` -> `{job}` |
| POST | `/api/resolve` | expand pasted paths (folders -> their MF4 files) |
| POST | `/api/export_csv` | body `{job, keys}` -> CSV identical to the CLI output |
| POST | `/api/upload?name=` | save a dropped file to a temp folder |
| POST | `/api/cache_clear` | delete all cached results |

A finished job can be reopened with `/?job=<id>` (also `&sel=<file>#<n>`, `&mode=corrected|raw`, `&zoom=release|debounce|full`).

---

## 9. Extending and limits

- **New channel or role**: add a key to `DEFAULT_SIGNALS`, read it through `SIG['key']` and a `PathSignals.get()` call,
  add a field to `StartupRow` (the CLI, the CSV and the GUI column list follow the dataclass automatically).
- **Different verdict thresholds**: use the CLI options, or change `classify()` / the combination block in `analyze_file()`.
- **Other recorders**: anything `asammdf` can open works as long as each path is its own channel group with a source path.
- **Resolution**: all differences have the logging resolution (`sample_period_ms`, typically 0.2 or 1 ms). With 1 ms
  logging both methods have +-0.5 ms uncertainty, which is why a disagreement involving `PB_BEFORE_PA` is `INCONCLUSIVE`.
- **Assumptions**: counters are re-initialised to 0 at power-up; the first transmitted IPC frame carries an alive
  counter of at least 1; both paths log the same IPC counters; a power-up shows as a `1 -> 0` edge of `init_done`.
- **Not covered**: more than two paths, recordings without the required channels (they are skipped, not guessed), and
  verdicts for power-ups where PATH2 is not released by the master (those are `INFO`).
