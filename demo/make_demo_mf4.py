"""
Builds a synthetic dual-path MF4 recording so the analyzer can be tried without any real vehicle / ECU data.

Run:  python demo/make_demo_mf4.py            ->  demo/demo_startups.mf4
Then: python analyze_mf4_startup_order.py demo/demo_startups.mf4

Channel names are the placeholders from config/signals.example.json. The recording contains seven power-ups that
cover every verdict the analyzer can report (clean order, same frame, PB before PA, resets on one / both paths,
maximum resets reached on both paths, system state 2 after release).

All times are integer sample indices (1 sample = 0.2 ms) so the expected result of every scenario is exact.
"""
import os
import sys
from dataclasses import dataclass
from typing import List

import numpy as np
from asammdf import MDF, Signal
from asammdf.blocks.source_utils import Source

DT = 0.0002                 # 0.2 ms logging period
MS = 5                      # samples per millisecond
LAT = 3                     # one-way IPC latency [samples] = 0.6 ms
GAP = 150                   # reboot data gap [samples] = 30 ms
SPACING = 15000             # distance between power-ups [samples] = 3 s
FIRST = 2500                # first power-up at 0.5 s


@dataclass
class Scenario:
    name: str
    delay: int              # ASW start of PATH2 minus PATH1 [samples]; negative -> PATH2 first
    offset: int             # PATH2 clock minus PATH1 clock [samples]
    resets1: int = 0
    resets2: int = 0
    max_resets2: bool = False
    state2: bool = False    # system state shows 2 for 4 ms after the PATH2 release


SCENARIOS: List[Scenario] = [
    Scenario("clean, PB 2.0 ms after PA", delay=10, offset=2),
    Scenario("clean, PB 0.8 ms after PA", delay=4, offset=3),
    Scenario("same frame", delay=0, offset=-2),
    Scenario("reset on PATH1 only (PA1-PB0)", delay=6, offset=3, resets1=1),
    Scenario("reset on both paths (PA1-PB1)", delay=3, offset=2, resets1=1, resets2=1),
    Scenario("PB before PA (violation) + state 2", delay=-6, offset=16, state2=True),
    Scenario("both paths reach max resets (PA3-PB3)", delay=5, offset=2, resets1=3, resets2=3, max_resets2=True),
]


def step(n: int, events) -> np.ndarray:
    """Piecewise-constant signal over n samples from [(start_index, value), ...]."""
    out = np.zeros(n, dtype=np.int64)
    ev = sorted(events)
    for (a, v), nxt in zip(ev, ev[1:] + [(n, 0)]):
        out[max(a, 0):max(min(nxt[0], n), 0)] = v
    return out


def alive(n: int, start: int, stop: int, every: int) -> np.ndarray:
    """Alive counter 1..250 increasing every `every` samples from `start`, 0 outside [start, stop)."""
    out = np.zeros(n, dtype=np.int64)
    k = np.arange(max(start, 0), min(stop, n))
    out[k] = 1 + ((k - start) // every) % 250
    return out


def delayed(x: np.ndarray, lag: int) -> np.ndarray:
    return np.r_[np.zeros(lag, dtype=x.dtype), x[:len(x) - lag]]


def build(path):
    n = FIRST + SPACING * len(SCENARIOS) + 6000
    p = {k: {"init_done": np.ones(n, np.int64), "error_counter": np.zeros(n, np.int64),
             "main_cnt": np.zeros(n, np.int64), "aux_cnt": np.zeros(n, np.int64),
             "hold_timeout": np.zeros(n, np.int64), "hold_cnt": np.zeros(n, np.int64),
             "sync_debounce": np.zeros(n, np.int64), "complete_latch": np.zeros(n, np.int64),
             "sys_state": np.zeros(n, np.int64), "valid": np.ones(n, bool)} for k in (1, 2)}
    tx = {k: {i: np.zeros(n, np.int64) for i in (0, 1, 9, 10, 18)} for k in (1, 2)}
    rx = {k: {i: np.zeros(n, np.int64) for i in (0, 1, 9, 10, 18)} for k in (1, 2)}
    expected = []

    for s_idx, sc in enumerate(SCENARIOS):
        t0 = FIRST + SPACING * s_idx
        end = t0 + SPACING
        seg = slice(t0, end)
        gaps = {1: [(t0, t0 + GAP)], 2: [(t0, t0 + GAP)]}
        err = {1: [], 2: []}
        for k, nres in ((1, sc.resets1), (2, sc.resets2)):
            for i in range(1, nres + 1):
                te = gaps[k][-1][1] + 100
                gaps[k].append((te, te + GAP))
                err[k].append((te, i))
        boot = {k: gaps[k][-1][1] for k in (1, 2)}
        rel1 = boot[1] + 150 + (30 if sc.resets1 == 0 else 0)
        if sc.max_resets2:
            rel2 = rel1 + 25
            err[2].append((rel2 - 2, 4))
        else:
            rel2 = max(rel1 + LAT + 2, boot[2] + 150)
        start1 = rel1 + 5
        start2 = start1 + sc.delay
        if sc.max_resets2:
            start2 = rel2 + 5      # PATH2 is released by the reset limit, not by the master
        rel = {1: rel1, 2: rel2}
        start = {1: start1, 2: start2}

        for k in (1, 2):
            d = p[k]
            d["init_done"][seg] = step(n, [(t0, 0), (rel[k], 1)])[seg]
            d["error_counter"][seg] = step(n, [(t0, 0)] + err[k] + [(rel[k], 0)])[seg]
            d["main_cnt"][seg] = alive(n, start[k], end, MS)[seg]
            d["aux_cnt"][seg] = alive(n, start[k], end, MS)[seg]
            d["sys_state"][seg] = step(n, [(t0, 6), (rel[k], 0)])[seg]
            for a, b in gaps[k]:
                d["valid"][a:b] = False
        d1 = p[1]
        ramp = np.clip((np.arange(n) - (rel1 - 26 * MS)) // MS, 0, 25)
        d1["sync_debounce"][seg] = np.where(np.arange(n) < rel1, ramp, 0)[seg]
        d1["hold_cnt"][seg] = np.where(np.arange(n) < rel1, np.clip((np.arange(n) - boot[1]) // MS, 0, 255), 0)[seg]
        if sc.state2:
            p[2]["sys_state"][seg] = step(n, [(t0, 6), (rel2, 2), (rel2 + 4 * MS, 0)])[seg]
        p[2]["complete_latch"][seg] = step(n, [(t0, 0), (rel2, 1)])[seg]

        # IPC frames in global time; the receiver sees them LAT samples later
        tx[1][0][seg] = alive(n, start1, end, 50 * MS)[seg]
        tx[1][1][seg] = alive(n, rel1, end, MS)[seg]
        tx[1][18][seg] = alive(n, rel1, end, 10_000)[seg]
        tx[2][9][seg] = alive(n, start2, end, 50 * MS)[seg]
        tx[2][10][seg] = alive(n, rel2, end, MS)[seg]
        for src, dst, idx in ((1, 2, 0), (1, 2, 1), (1, 2, 18), (2, 1, 9), (2, 1, 10)):
            rx[dst][idx][seg] = delayed(tx[src][idx], LAT)[seg]

        expected.append((sc.name, sc.delay * DT * 1e3))

    # a power-up closes the previous segment: counters restart at 0 (already zero at the segment start)
    sources = {1: Source(name="PATH1", path="Demo/PATH1/Measurement", comment="", source_type=Source.SOURCE_ECU,
                         bus_type=Source.BUS_TYPE_NONE),
               2: Source(name="PATH2", path="Demo/PATH2/Measurement", comment="", source_type=Source.SOURCE_ECU,
                         bus_type=Source.BUS_TYPE_NONE)}
    names = {"init_done": "Ctrl_InitDone_bl", "error_counter": "Ctrl_ResetCounter_u8", "main_cnt": "App_MainCounter_u8",
             "aux_cnt": "App_AuxCounter_u8", "hold_timeout": "Ctrl_HoldTimeout_bl", "hold_cnt": "Ctrl_HoldCnt_u16",
             "sync_debounce": "Ctrl_SyncDebounce_u8", "complete_latch": "Ipc_ResetCompleteLatch_bl",
             "sys_state": "SysState_u8"}
    dtypes = {"hold_cnt": np.uint16}

    mdf = MDF(version="4.10")
    for k, off in ((1, 0), (2, None)):
        d = p[k]
        valid = d["valid"]
        offset_s = 0.0 if k == 1 else float(np.median([sc.offset for sc in SCENARIOS])) * DT
        # the PATH2 clock runs ahead by `offset` (per-scenario offsets are applied on the timestamps below)
        ts = np.arange(n)[valid] * DT
        if k == 2:
            shift = np.zeros(n)
            for s_idx, sc in enumerate(SCENARIOS):
                shift[FIRST + SPACING * s_idx:FIRST + SPACING * (s_idx + 1)] = sc.offset * DT
            ts = ts + shift[valid]
        sigs = []
        for key, nm in names.items():
            sigs.append(Signal(d[key][valid].astype(dtypes.get(key, np.uint8)), ts, name=nm, source=sources[k]))
        for i in tx[k]:
            sigs.append(Signal(tx[k][i][valid].astype(np.uint8), ts, name=f"Ipc_TxCounter_au8._{i}_", source=sources[k]))
            sigs.append(Signal(rx[k][i][valid].astype(np.uint8), ts, name=f"Ipc_RxCounter_au8._{i}_", source=sources[k]))
        mdf.append(sigs, comment=f"PATH{k}")
    mdf.save(path, overwrite=True)
    return expected


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                            "demo_startups.mf4")
    exp = build(out)
    print(f"Wrote {out}")
    print("Expected PB - PA start difference per power-up [ms]:")
    for i, (name, d) in enumerate(exp, 1):
        print(f"  {i}. {d:+5.1f}  {name}")
