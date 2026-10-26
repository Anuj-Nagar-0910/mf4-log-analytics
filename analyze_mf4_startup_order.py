"""
Start-up order analysis of a dual-path controller: PATH1 (PA, master) vs PATH2 (PB, slave), from MF4 / MDF4 recordings.

One CSV row per start-up (power-up), independent of how many resets each path performed in it
(PA1-PB0, PA1-PB1, PA2-PB0, PA3-PB3, ...).

Per start-up it reports
  - raw times (first update of the main application counter per path),
  - the PATH1/PATH2 clock offset of the recording, measured from IPC counter pairs (cyclic reference message both ways),
    and the offset-corrected PB - PA start difference,
  - an offset-free PB - PA start difference (each path's own-clock delay between its first 'application running' IPC frame
    and the sibling's first 'application running' IPC frame),
  - the handshake (master RESET_COMPLETE TX/RX) and why each path released,
  - whether the system-state signal showed value 2 after release.

Channel names are not hard-coded: they are looked up in a signal map (see config/signals.example.json).

Sign convention everywhere: diff = PATH2 - PATH1 (positive -> PB started after PA).
"""
import os
import glob
import json
import argparse
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from asammdf import MDF

# IPC message indices (index into the Tx / Rx alive-counter arrays; overridable in the signal map)
IPC_MOV_A = 0           # PATH1 -> PATH2, sent only while the application runs
IPC_TRQ_A = 1           # PATH1 -> PATH2, every 1 ms (clock offset reference)
IPC_MOV_B = 9           # PATH2 -> PATH1, sent only while the application runs
IPC_TRQ_B = 10          # PATH2 -> PATH1, every 1 ms (clock offset reference)
IPC_COMPLETE_A = 18     # PATH1 RESET_COMPLETE -> PATH2

SYNC_DEBOUNCE_TICKS = 25
REBOOT_GAP_S = 0.020          # VX1000 data gap that indicates a path reset / power-up
BOUNDARY_MERGE_S = 1.0        # power-up edges of both paths closer than this belong to one start-up
OFFSET_PAIR_MATCH_S = 0.050   # same counter value searched within +-50 ms
MIN_OFFSET_PAIRS = 5

NAN = float('nan')

# ----------------------------------------------------------------------------------------------------------------------
# Signal map: logical role -> channel name in the recording
# ----------------------------------------------------------------------------------------------------------------------
DEFAULT_SIGNALS = {
    'path1_marker': 'PATH1',                # substring of the acquisition source path that identifies path 1 / path 2
    'path2_marker': 'PATH2',
    'init_done': 'Ctrl_InitDone_bl',        # 1 once the path finished its init (release); 1 -> 0 = power-up (RAM re-init)
    'error_counter': 'Ctrl_ResetCounter_u8',
    'main_cnt': 'App_MainCounter_u8',       # counter that starts counting when the application runs
    'aux_cnt': 'App_AuxCounter_u8',
    'hold_timeout': 'Ctrl_HoldTimeout_bl',
    'hold_cnt': 'Ctrl_HoldCnt_u16',
    'sync_debounce': 'Ctrl_SyncDebounce_u8',
    'complete_latch': 'Ipc_ResetCompleteLatch_bl',
    'sys_state': 'SysState_u8',             # system state broadcast; value 2 is checked after release
    'ipc_tx': 'Ipc_TxCounter_au8',          # alive-counter arrays (field '<name>._N_')
    'ipc_rx': 'Ipc_RxCounter_au8',
}


def load_signals(path: Optional[str] = None) -> Dict[str, str]:
    """Defaults, overridden by (in order) config/signals.local.json, $MF4_SIGNALS_FILE, `path`.
    IPC message indices can be overridden with the keys ipc_mov_a, ipc_trq_a, ipc_mov_b, ipc_trq_b, ipc_complete_a."""
    here = os.path.dirname(os.path.abspath(__file__))
    sig = dict(DEFAULT_SIGNALS)
    for cand in (os.path.join(here, 'config', 'signals.local.json'), os.environ.get('MF4_SIGNALS_FILE'), path):
        if cand and os.path.isfile(cand):
            with open(cand, 'r', encoding='utf-8') as fh:
                sig.update({k: v for k, v in json.load(fh).items() if not k.startswith('_')})
    for key, const in (('ipc_mov_a', 'IPC_MOV_A'), ('ipc_trq_a', 'IPC_TRQ_A'), ('ipc_mov_b', 'IPC_MOV_B'),
                       ('ipc_trq_b', 'IPC_TRQ_B'), ('ipc_complete_a', 'IPC_COMPLETE_A')):
        if key in sig:
            globals()[const] = int(sig[key])
    return sig


SIG: Dict[str, str] = {}
SIG.update(load_signals())



# ----------------------------------------------------------------------------------------------------------------------
# Signal access
# ----------------------------------------------------------------------------------------------------------------------
class PathSignals:
    """Lazy channel loader for one VX1000 path group."""

    def __init__(self, mdf: MDF, group: int, label: str):
        self.mdf = mdf
        self.group = group
        self.label = label
        self._cache: Dict[str, Optional[Tuple[np.ndarray, np.ndarray]]] = {}
        self._warned = set()

    def _load(self, name: str) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        try:
            sig = self.mdf.get(name, group=self.group, raw=True)
            return sig.timestamps, np.asarray(sig.samples).astype(np.int64)
        except Exception:
            return None

    def _load_array_field(self, parent: str, index: int) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """The IPC Rx counter array may be logged as a structured channel (field '<parent>._N_') or as split channels."""
        direct = self._load(f"{parent}._{index}_")
        if direct is not None:
            return direct
        try:
            sig = self.mdf.get(parent, group=self.group, raw=True)
        except Exception:
            return None
        names = sig.samples.dtype.names or ()
        wanted = f"{parent}._{index}_"
        if wanted in names:
            return sig.timestamps, np.asarray(sig.samples[wanted]).astype(np.int64)
        return None

    def get(self, name: str) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        if name not in self._cache:
            self._cache[name] = self._load(name)
            if self._cache[name] is None and name not in self._warned:
                self._warned.add(name)
                print(f"    [warn] {self.label}: channel '{name}' not found -> related columns NaN")
        return self._cache[name]

    def ipc(self, direction: str, index: int) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        parent = SIG['ipc_tx' if direction == 'Tx' else 'ipc_rx']
        key = f"{parent}[{index}]"
        if key not in self._cache:
            self._cache[key] = self._load_array_field(parent, index)
            if self._cache[key] is None and key not in self._warned:
                self._warned.add(key)
                print(f"    [warn] {self.label}: '{key}' not found -> related columns NaN")
        return self._cache[key]


def edge_indices(v: np.ndarray) -> np.ndarray:
    return np.flatnonzero(v[1:] != v[:-1]) + 1


def first_change(sig, a: float, b: float) -> float:
    """Timestamp of the first change to a non-zero value within [a, b].
    Counters (mov/mor, IPC alive counters) are re-initialised to 0 at power-up / reset; that is not an event.
    The first transmitted / received IPC frame carries alive counter >= 1."""
    if sig is None or np.isnan(a) or np.isnan(b):
        return NAN
    t, v = sig
    idx = edge_indices(v)
    sel = idx[(t[idx] >= a) & (t[idx] <= b) & (v[idx] != 0)]
    return float(t[sel[0]]) if len(sel) else NAN


def value_at(sig, x: float) -> float:
    if sig is None or np.isnan(x):
        return NAN
    t, v = sig
    j = np.searchsorted(t, x, side='right') - 1
    return float(v[j]) if j >= 0 else NAN


def rising_edges(sig, a: float, b: float) -> np.ndarray:
    """0 -> !=0 transitions within [a, b)."""
    if sig is None:
        return np.array([])
    t, v = sig
    idx = edge_indices(v)
    idx = idx[(v[idx] != 0) & (v[idx - 1] == 0)]
    tt = t[idx]
    return tt[(tt >= a) & (tt < b)]


def falling_edges(sig) -> np.ndarray:
    """!=0 -> 0 transitions."""
    if sig is None:
        return np.array([])
    t, v = sig
    idx = edge_indices(v)
    idx = idx[(v[idx] == 0) & (v[idx - 1] != 0)]
    return t[idx]


def last_reboot_end(sig, a: float, b: float) -> float:
    """End of the last VX1000 data gap (path reset / power-up) within [a, b]; a if none."""
    if sig is None:
        return a
    t, _ = sig
    sel = np.flatnonzero((t >= a) & (t <= b))
    if len(sel) < 2:
        return a
    tt = t[sel]
    gaps = np.flatnonzero(np.diff(tt) > REBOOT_GAP_S)
    return float(tt[gaps[-1] + 1]) if len(gaps) else a


def ipc_lag(tx_sig, rx_sig, a: float, b: float) -> Tuple[np.ndarray, np.ndarray]:
    """(send times, receive time - send time) of the same alive-counter value, send edges in [a, b]."""
    if tx_sig is None or rx_sig is None:
        return np.array([]), np.array([])
    tt, tv = tx_sig
    rt, rv = rx_sig
    ti = edge_indices(tv)
    ti = ti[(tt[ti] >= a) & (tt[ti] <= b)]
    ri = edge_indices(rv)
    ri = ri[(rt[ri] >= a - 2 * OFFSET_PAIR_MATCH_S) & (rt[ri] <= b + 2 * OFFSET_PAIR_MATCH_S)]
    r_t, r_v = rt[ri], rv[ri]
    times, lags = [], []
    for k in ti:
        cand = np.flatnonzero((r_v == tv[k]) & (np.abs(r_t - tt[k]) <= OFFSET_PAIR_MATCH_S))
        if len(cand):
            d = r_t[cand] - tt[k]
            times.append(tt[k])
            lags.append(d[np.argmin(np.abs(d))])
    return np.asarray(times), np.asarray(lags)


def lag_at(times: np.ndarray, lags: np.ndarray, t_eval: float) -> float:
    """Lag evaluated at t_eval. The PATH1/PATH2 clock offset can drift by several ms/s right after power-up,
    so a linear fit over the window is used (median if too few points / too short span)."""
    if len(lags) == 0:
        return NAN
    if len(lags) >= MIN_OFFSET_PAIRS and np.ptp(times) > 0.010:
        slope, icpt = np.polyfit(times - times[0], lags, 1)
        return float(icpt + slope * (t_eval - times[0]))
    return float(np.median(lags))


def stsys2_duration(sig, a: float, b: float) -> Tuple[str, float]:
    """Whether the system state == 2 occurs in [a, b] and for how long (ms, sample based)."""
    if sig is None or np.isnan(a):
        return 'NA', NAN
    t, v = sig
    sel = np.flatnonzero((t >= a) & (t <= b))
    if len(sel) == 0:
        return 'NA', NAN
    j0 = max(sel[0] - 1, 0)
    sel = np.r_[j0, sel] if j0 != sel[0] else sel
    is2 = v[sel] == 2
    if not is2.any():
        return 'N', 0.0
    dt = np.diff(np.r_[t[sel], b])
    dur = float(np.sum(dt[is2])) * 1e3
    return 'Y', round(dur, 3)


def stsys_segments(sig, a: float, b: float) -> List[Tuple[float, float, int]]:
    """System-state value segments [(t0, t1, value)] in [a, b] (sample-and-hold, equal values merged).
    The value valid at `a` is taken from the last sample at or before `a`."""
    if sig is None or np.isnan(a) or np.isnan(b):
        return []
    t, v = sig
    sel = np.flatnonzero((t > a) & (t <= b))
    j0 = np.searchsorted(t, a, side='right') - 1
    pts = ([(a, int(v[j0]))] if j0 >= 0 else []) + [(float(t[k]), int(v[k])) for k in sel]
    if not pts:
        return []
    segs = []
    for (t0, val), nxt in zip(pts, pts[1:] + [(b, None)]):
        if segs and segs[-1][2] == val:
            segs[-1][1] = nxt[0]
        else:
            segs.append([t0, nxt[0], val])
    return [(float(s[0]), float(s[1]), int(s[2])) for s in segs]


def stsys_sequence(sig, a: float, b: float) -> str:
    """'6>2(4.0ms)>0': value sequence in [a, b]; duration for every segment but the last (open-ended) one."""
    if sig is None:
        return 'NA'
    segs = stsys_segments(sig, a, b)
    if not segs:
        return 'NA'
    parts = [f"{val}({(t1 - t0) * 1e3:.1f}ms)" for t0, t1, val in segs[:-1]] + [str(segs[-1][2])]
    return '>'.join(parts)


def ms(x: float) -> float:
    return NAN if x is None or np.isnan(x) else round(x * 1e3, 3)


# ----------------------------------------------------------------------------------------------------------------------
# Result row
# ----------------------------------------------------------------------------------------------------------------------
@dataclass
class StartupRow:
    file_name: str
    startup_idx: int
    segment_start: float
    sample_period_ms: float = NAN       # VX1000 logging period (1 ms or 0.2 ms) -> resolution of all diffs
    scenario: str = ''
    reset_paths: str = ''
    trigger_path: str = ''              # old column (= reset_paths)
    resets_p1: int = 0
    resets_p2: int = 0
    max_resets_p1: str = 'N'
    max_resets_p2: str = 'N'
    error_counter_time: float = NAN     # old column: first reset (either path)
    p1_first_error_time: float = NAN
    p2_first_error_time: float = NAN
    p1_release_time: float = NAN
    p2_release_time: float = NAN
    p1_release_reason: str = ''
    p2_release_reason: str = ''
    p1_sync_debounce_at_release: float = NAN
    p1_hold_cnt_at_release: float = NAN
    p2_hold_cnt_at_release: float = NAN
    p1_mov_start_time: float = NAN
    p2_mov_start_time: float = NAN
    path1_mor_inc_time: float = NAN     # old column
    path2_mor_inc_time: float = NAN     # old column
    time_difference_sec: float = NAN    # old column: raw P2 mor - P1 mor
    raw_diff_mor_ms: float = NAN
    raw_diff_mov_ms: float = NAN
    clock_offset_ms: float = NAN
    ipc_latency_ms: float = NAN
    offset_pairs_n: int = 0
    corrected_diff_mor_ms: float = NAN
    corrected_diff_mov_ms: float = NAN
    p1_own_mov_tx_time: float = NAN
    p1_rx_sibling_mov_time: float = NAN
    p2_own_mov_tx_time: float = NAN
    p2_rx_sibling_mov_time: float = NAN
    p1_ownclock_rx_delay_ms: float = NAN
    p2_ownclock_rx_delay_ms: float = NAN
    offsetfree_diff_ms: float = NAN
    offsetfree_latency_ms: float = NAN
    complete_tx_p1_time: float = NAN
    complete_rx_p2_time: float = NAN
    p1_mor_running_before_release: str = ''
    p2_mor_running_before_release: str = ''
    p1_stsys2: str = ''
    p1_stsys2_ms: float = NAN
    p2_stsys2: str = ''
    p2_stsys2_ms: float = NAN
    p1_stsys_sequence: str = ''         # e.g. '6>2(4.0ms)>0' in [release, release + StSys window]
    p2_stsys_sequence: str = ''
    corrected_verdict: str = ''
    offsetfree_verdict: str = ''
    Order_Verdict: str = ''
    order_rule: str = ''
    order_rule_reason: str = ''
    Order_Result: str = ''
    Offset_Flag: str = ''
    Flag: str = ''
    Negative_Flag: str = ''


# ----------------------------------------------------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------------------------------------------------
def find_path_groups(mdf: MDF) -> Tuple[Optional[int], Optional[int]]:
    """A path group is a channel group that holds the reset counter and the main counter; its acquisition source path names the path (markers: path1_marker / path2_marker)."""
    p1, p2 = None, None
    for g_idx, group in enumerate(mdf.groups):
        has_error, has_mor, source = False, False, None
        for ch in group.channels:
            name = getattr(ch, 'name', str(ch))
            if name == SIG['error_counter']:
                has_error = True
                source = ch.source.path if ch.source else None
            elif name == SIG['main_cnt']:
                has_mor = True
        if has_error and has_mor and source:
            if SIG['path1_marker'].upper() in source.upper():
                p1 = g_idx
            elif SIG['path2_marker'].upper() in source.upper():
                p2 = g_idx
    return p1, p2


def segment_boundaries(p1: PathSignals, p2: PathSignals) -> List[float]:
    """Power-up = init-done flag 1 -> 0 (RAM re-init) on either path; merged within 1 s."""
    edges = np.sort(np.r_[falling_edges(p1.get(SIG['init_done'])),
                          falling_edges(p2.get(SIG['init_done']))])
    starts = []
    for x in edges:
        if not starts or x - starts[-1] > BOUNDARY_MERGE_S:
            starts.append(float(x))
    t0 = min(p1.get(SIG['init_done'])[0][0], p2.get(SIG['init_done'])[0][0])
    if not starts or starts[0] - t0 > BOUNDARY_MERGE_S:
        starts.insert(0, float(t0))
    return starts


def count_resets(sig, a: float, b: float) -> Tuple[int, str, float]:
    """Steps of the reset counter into 1, 2, 3 (one per reset) and whether 4 (max resets) was reached."""
    if sig is None:
        return 0, 'NA', NAN
    t, v = sig
    idx = edge_indices(v)
    idx = idx[(t[idx] >= a) & (t[idx] < b)]
    up = idx[v[idx] > v[idx - 1]]
    resets = up[(v[up] >= 1) & (v[up] <= 3)]
    reached4 = up[v[up] == 4]
    first = float(t[resets[0]]) if len(resets) else NAN
    return int(len(resets)), ('Y' if len(reached4) else 'N'), first


def release_reason(path: PathSignals, rel: float, seg_start: float, is_p2: bool, complete_rx: float) -> str:
    if np.isnan(rel):
        return 'NO_RELEASE'
    # release sets the counter to 0 (or 4) in the same call -> use the resets counted in this start-up
    n_resets, reached4, _ = count_resets(path.get(SIG['error_counter']), seg_start, rel + 0.002)
    if reached4 == 'Y':
        return 'MAX_RESETS'
    if n_resets >= 2:
        return 'NO_WAIT'        # counter > 1 at release: no wait for the sibling
    if value_at(path.get(SIG['hold_timeout']), rel) == 1:
        return 'TIMEOUT_300MS'
    if is_p2:
        # RX counter and release flag may land in neighbouring samples -> +-1 sample (<= 1 ms) tolerance;
        # the IPC RESET_COMPLETE latch set at release is accepted as the same evidence
        rx_ok = not np.isnan(complete_rx) and rel - 0.002 <= complete_rx <= rel + 0.001
        latch_ok = value_at(path.get(SIG['complete_latch']), rel + 1e-6) == 1
        if rx_ok or latch_ok:
            return 'MASTER_COMPLETE'
    else:
        deb = path.get(SIG['sync_debounce'])
        if deb is not None:
            t, v = deb
            sel = np.flatnonzero((t >= rel - 0.005) & (t <= rel))
            if len(sel) and v[sel].max() >= SYNC_DEBOUNCE_TICKS:
                return 'DEBOUNCE_25MS'
    return 'OTHER'


def classify(d: float, tol: float) -> str:
    if d is None or np.isnan(d):
        return 'NA'
    if d > tol:
        return 'PA_FIRST'
    if d < -tol:
        return 'PB_BEFORE_PA'
    return 'SAME_FRAME'


def _jf(x) -> Optional[float]:
    """float -> JSON-safe value (NaN -> None)."""
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), 6)


def collect_timeline(p: PathSignals, seg_a: float, rel: float, lim: float, st_win: float, is_p1: bool) -> dict:
    """Event lists of one path in one start-up for drawing (own clock). Not part of the CSV."""
    out = {'errors': [], 'gaps': [], 'debounce_start': None, 'timeout_marker': None, 'stsys2': [], 'stsys_segments': []}
    cnt = p.get(SIG['error_counter'])
    if cnt is not None:
        t, v = cnt
        idx = edge_indices(v)
        idx = idx[(t[idx] >= seg_a) & (t[idx] <= lim)]
        out['errors'] = [[_jf(t[k]), int(v[k - 1]), int(v[k])] for k in idx if 1 <= v[k] <= 4 and v[k] > v[k - 1]]
    mor = p.get(SIG['main_cnt'])
    if mor is not None:
        t, _ = mor
        sel = np.flatnonzero((t >= seg_a) & (t <= lim))
        if len(sel) > 1:
            tt = t[sel]
            g = np.flatnonzero(np.diff(tt) > REBOOT_GAP_S)
            out['gaps'] = [[_jf(tt[k]), _jf(tt[k + 1])] for k in g]
    if is_p1 and not np.isnan(rel):
        deb = p.get(SIG['sync_debounce'])
        if deb is not None:
            ups = rising_edges(deb, seg_a, rel + 1e-6)
            out['debounce_start'] = _jf(ups[-1]) if len(ups) else None
    mk = rising_edges(p.get(SIG['hold_timeout']), seg_a, lim)
    out['timeout_marker'] = _jf(mk[0]) if len(mk) else None
    st = p.get(SIG['sys_state'])
    if st is not None and not np.isnan(rel):
        t, v = st
        sel = np.flatnonzero((t >= rel) & (t <= rel + st_win))
        start = None
        for k in sel:
            if v[k] == 2 and start is None:
                start = t[k]
            elif v[k] != 2 and start is not None:
                out['stsys2'].append([_jf(start), _jf(t[k])])
                start = None
        if start is not None:
            out['stsys2'].append([_jf(start), _jf(rel + st_win)])
        # all values, from shortly before release (shows Init 6) to the end of the StSys window
        out['stsys_segments'] = [[_jf(a), _jf(b), val] for a, b, val in
                                 stsys_segments(st, max(seg_a, rel - 0.050), rel + st_win)]
    return out


def analyze_file(file_path: str, args, timelines: Optional[dict] = None) -> List[StartupRow]:
    """timelines: optional dict, filled with '<file>#<startup_idx>' -> event lists for the GUI timeline view."""
    file_name = os.path.basename(file_path)
    print(f"Analyzing {file_name}...")
    try:
        mdf = MDF(file_path)
    except Exception as e:
        print(f"  Error loading {file_name}: {e}")
        return []

    g1, g2 = find_path_groups(mdf)
    if g1 is None or g2 is None:
        print(f"  Skipping {file_name}: could not find both PATH1 and PATH2 groups.")
        return []
    p1 = PathSignals(mdf, g1, 'PATH1')
    p2 = PathSignals(mdf, g2, 'PATH2')
    for p in (p1, p2):
        for req in (SIG['init_done'], SIG['error_counter'], SIG['main_cnt']):
            if p.get(req) is None:
                print(f"  Skipping {file_name}: required channel '{req}' missing on {p.label}.")
                return []

    win = args.window_ms / 1e3
    st_win = args.stsys_window_ms / 1e3
    tol = args.same_frame_tol_ms
    starts = segment_boundaries(p1, p2)
    t_end = max(p1.get(SIG['main_cnt'])[0][-1], p2.get(SIG['main_cnt'])[0][-1]) + 1.0

    rows: List[StartupRow] = []
    for k, seg_a in enumerate(starts):
        seg_b = starts[k + 1] if k + 1 < len(starts) else t_end

        rel1s = rising_edges(p1.get(SIG['init_done']), seg_a, seg_b)
        rel2s = rising_edges(p2.get(SIG['init_done']), seg_a, seg_b)
        rel1 = float(rel1s[0]) if len(rel1s) else NAN
        rel2 = float(rel2s[0]) if len(rel2s) else NAN
        lim1 = seg_b if np.isnan(rel1) else rel1 + 0.002
        lim2 = seg_b if np.isnan(rel2) else rel2 + 0.002
        n1, max1, err1 = count_resets(p1.get(SIG['error_counter']), seg_a, lim1)
        n2, max2, err2 = count_resets(p2.get(SIG['error_counter']), seg_a, lim2)
        if np.isnan(rel1) and np.isnan(rel2) and n1 == 0 and n2 == 0:
            continue    # recording started/ended mid-run, nothing to evaluate

        row = StartupRow(file_name=file_name, startup_idx=len(rows) + 1, segment_start=round(seg_a, 6))
        row.resets_p1, row.resets_p2 = n1, n2
        row.max_resets_p1, row.max_resets_p2 = max1, max2
        row.scenario = f"PA{n1}-PB{n2}"
        row.reset_paths = {(False, False): 'NONE', (True, False): 'P1',
                           (False, True): 'P2', (True, True): 'BOTH'}[(n1 > 0, n2 > 0)]
        row.trigger_path = row.reset_paths
        row.p1_first_error_time, row.p2_first_error_time = err1, err2
        row.error_counter_time = float(np.nanmin([err1, err2])) if not (np.isnan(err1) and np.isnan(err2)) else NAN
        row.p1_release_time, row.p2_release_time = rel1, rel2
        mt = p1.get(SIG['main_cnt'])[0]
        around = mt[(mt >= (seg_a if np.isnan(rel1) else rel1) - 0.05) & (mt <= (seg_b if np.isnan(rel1) else rel1) + 0.05)]
        row.sample_period_ms = round(float(np.median(np.diff(around))) * 1e3, 3) if len(around) > 2 else NAN

        # ---- raw ASW start (first mov / mor update) ---------------------------------------------------------------
        def start_of(p: PathSignals, rel: float, name: str) -> float:
            return NAN if np.isnan(rel) else first_change(p.get(name), rel - 0.005, rel + win)
        row.p1_mov_start_time = start_of(p1, rel1, SIG['aux_cnt'])
        row.p2_mov_start_time = start_of(p2, rel2, SIG['aux_cnt'])
        row.path1_mor_inc_time = start_of(p1, rel1, SIG['main_cnt'])
        row.path2_mor_inc_time = start_of(p2, rel2, SIG['main_cnt'])
        raw_mor = row.path2_mor_inc_time - row.path1_mor_inc_time
        raw_mov = row.p2_mov_start_time - row.p1_mov_start_time
        row.time_difference_sec = raw_mor
        row.raw_diff_mor_ms, row.raw_diff_mov_ms = ms(raw_mor), ms(raw_mov)

        # MOR active before the own release (latent no-init activation flag) ?
        for p, rel, attr in ((p1, rel1, 'p1_mor_running_before_release'), (p2, rel2, 'p2_mor_running_before_release')):
            if np.isnan(rel):
                setattr(row, attr, 'NA')
                continue
            boot = last_reboot_end(p.get(SIG['main_cnt']), seg_a, rel)
            early = first_change(p.get(SIG['main_cnt']), boot + 0.001, rel - 0.005)
            setattr(row, attr, 'N' if np.isnan(early) else 'Y')

        # ---- clock offset from IPC TRQ counter pairs -------------------------------------------------------------
        if not (np.isnan(rel1) and np.isnan(rel2)):
            ref = float(np.nanmax([rel1, rel2]))
            # evaluate the offset at the moment of the ASW start (drift compensated)
            t_eval = row.path1_mor_inc_time if not np.isnan(row.path1_mor_inc_time) else ref
            for span in (0.105, 0.300):
                tab, gab = ipc_lag(p1.ipc('Tx', IPC_TRQ_A), p2.ipc('Rx', IPC_TRQ_A),
                                   ref + 0.005, ref + span)
                tba, gba = ipc_lag(p2.ipc('Tx', IPC_TRQ_B), p1.ipc('Rx', IPC_TRQ_B),
                                   ref + 0.005, ref + span)
                nab, nba = len(gab), len(gba)
                if min(nab, nba) >= MIN_OFFSET_PAIRS:
                    break
            if min(nab, nba) > 0:
                lab, lba = lag_at(tab, gab, t_eval), lag_at(tba, gba, t_eval)
                off = (lab - lba) / 2.0
                row.clock_offset_ms = ms(off)
                row.ipc_latency_ms = ms((lab + lba) / 2.0)
                row.offset_pairs_n = int(min(nab, nba))
                row.corrected_diff_mor_ms = ms(raw_mor - off)
                row.corrected_diff_mov_ms = ms(raw_mov - off)

        # ---- offset-free: own MOV frame #1 vs sibling MOV frame #1, each on its own clock -----------------------
        if not np.isnan(rel1) and not np.isnan(rel2):
            b1 = last_reboot_end(p1.get(SIG['main_cnt']), seg_a, rel1)
            b2 = last_reboot_end(p2.get(SIG['main_cnt']), seg_a, rel2)
            row.p1_own_mov_tx_time = first_change(p1.ipc('Tx', IPC_MOV_A), rel1 - 0.005, rel1 + win)
            row.p2_own_mov_tx_time = first_change(p2.ipc('Tx', IPC_MOV_B), rel2 - 0.005, rel2 + win)
            row.p1_rx_sibling_mov_time = first_change(p1.ipc('Rx', IPC_MOV_B), b1 + 0.001, rel1 + 1.0)
            row.p2_rx_sibling_mov_time = first_change(p2.ipc('Rx', IPC_MOV_A), b2 + 0.001, rel2 + 1.0)
            da = row.p1_rx_sibling_mov_time - row.p1_own_mov_tx_time     # = (B start - A start) + latency
            db = row.p2_rx_sibling_mov_time - row.p2_own_mov_tx_time     # = (A start - B start) + latency
            row.p1_ownclock_rx_delay_ms, row.p2_ownclock_rx_delay_ms = ms(da), ms(db)
            row.offsetfree_diff_ms = ms((da - db) / 2.0)                  # B start - A start
            row.offsetfree_latency_ms = ms((da + db) / 2.0)

        # ---- handshake and release reasons -----------------------------------------------------------------------
        # search after the last reboot of the path: the counter re-init at power-up / reset is not a transmission
        row.complete_tx_p1_time = first_change(p1.ipc('Tx', IPC_COMPLETE_A), last_reboot_end(
            p1.get(SIG['main_cnt']), seg_a, lim1) + 0.001, lim1)
        row.complete_rx_p2_time = first_change(p2.ipc('Rx', IPC_COMPLETE_A), last_reboot_end(
            p2.get(SIG['main_cnt']), seg_a, lim2) + 0.001, lim2)
        row.p1_sync_debounce_at_release = value_at(p1.get(SIG['sync_debounce']), rel1 - 1e-6)
        row.p1_hold_cnt_at_release = value_at(p1.get(SIG['hold_cnt']), rel1)
        row.p2_hold_cnt_at_release = value_at(p2.get(SIG['hold_cnt']), rel2)
        row.p1_release_reason = release_reason(p1, rel1, seg_a, False, NAN)
        row.p2_release_reason = release_reason(p2, rel2, seg_a, True, row.complete_rx_p2_time)

        # ---- system state --------------------------------------------------------------------------------------------
        row.p1_stsys2, row.p1_stsys2_ms = stsys2_duration(p1.get(SIG['sys_state']), rel1, rel1 + st_win)
        row.p2_stsys2, row.p2_stsys2_ms = stsys2_duration(p2.get(SIG['sys_state']), rel2, rel2 + st_win)
        row.p1_stsys_sequence = stsys_sequence(p1.get(SIG['sys_state']), rel1, rel1 + st_win)
        row.p2_stsys_sequence = stsys_sequence(p2.get(SIG['sys_state']), rel2, rel2 + st_win)

        off, lat = row.clock_offset_ms, row.ipc_latency_ms
        if np.isnan(off):
            row.Offset_Flag = 'NO_OFFSET'
        elif abs(off) > args.offset_suspect_ms or lat < 0 or lat > 2.0:
            row.Offset_Flag = 'OFFSET_SUSPECT'
        else:
            row.Offset_Flag = 'OK'

        # ---- verdicts ---------------------------------------------------------------------------------------------
        row.corrected_verdict = classify(row.corrected_diff_mor_ms, tol)
        # offset-free result is only usable if the derived one-way IPC latency is physical (0..2 ms)
        fl = row.offsetfree_latency_ms
        free_ok = not np.isnan(fl) and 0.0 <= fl <= 2.0 and abs(row.offsetfree_diff_ms) <= 50.0
        row.offsetfree_verdict = classify(row.offsetfree_diff_ms, tol) if free_ok else 'NA'
        cv, fv = row.corrected_verdict, row.offsetfree_verdict
        if cv == 'NA' and fv == 'NA':
            row.Order_Verdict = 'INCONCLUSIVE'
        elif cv == 'NA':
            row.Order_Verdict = fv
        elif fv == 'NA' or cv == fv:
            row.Order_Verdict = cv
        elif 'PB_BEFORE_PA' in (cv, fv):
            # PB_BEFORE_PA is only reported when both methods agree; otherwise manual check (e.g. 1 ms logging:
            # both methods have +-0.5 ms resolution)
            row.Order_Verdict = 'INCONCLUSIVE'
        elif row.Offset_Flag == 'OFFSET_SUSPECT':
            row.Order_Verdict = fv      # PA_FIRST vs SAME_FRAME with a large / drifting offset -> clock-independent
        else:
            row.Order_Verdict = cv      # PA_FIRST vs SAME_FRAME (one sample apart) -> corrected value decides

        if row.p2_release_reason == 'MASTER_COMPLETE':
            row.order_rule, row.order_rule_reason = 'ENFORCED', 'PB_ON_MASTER_COMPLETE'
        else:
            row.order_rule = 'NOT_APPLICABLE'
            row.order_rule_reason = 'NO_RELEASE' if row.p2_release_reason == 'NO_RELEASE' \
                else 'PB_' + row.p2_release_reason
        if row.order_rule != 'ENFORCED':
            row.Order_Result = 'INFO'
        elif row.Order_Verdict == 'PB_BEFORE_PA':
            row.Order_Result = 'NOK'
        elif row.Order_Verdict == 'INCONCLUSIVE':
            row.Order_Result = 'CHECK'
        else:
            row.Order_Result = 'OK'

        if np.isnan(raw_mor):
            row.Flag, row.Negative_Flag = 'NA', 'NA'
        else:
            row.Flag = 'NOK' if abs(raw_mor) > 0.005 else 'OK'
            row.Negative_Flag = 'NEGATIVE' if raw_mor < 0 else 'OK'

        if timelines is not None:
            timelines[f"{file_name}#{row.startup_idx}"] = {
                'seg_start': _jf(seg_a), 'seg_end': _jf(min(seg_b, float(np.nanmax([lim1, lim2])) + st_win)),
                'p1': collect_timeline(p1, seg_a, rel1, lim1, st_win, True),
                'p2': collect_timeline(p2, seg_a, rel2, lim2, st_win, False),
            }

        rows.append(row)

    print(f"  {len(rows)} start-up(s) found.")
    return rows


# ----------------------------------------------------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------------------------------------------------
def main():
    here = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(
        description="Start-up order PATH1 vs PATH2 per start-up (offset-corrected and offset-free), CSV output.")
    parser.add_argument("target", nargs='?', default=os.path.join(here, "Analysis_file_1.mf4"),
                        help="Path to a single .mf4 file or a directory containing .mf4 files.")
    parser.add_argument("--output", default=None,
                        help="Output CSV (default: <mf4 name>_Startup_Order.csv or Startup_Order_Analysis.csv "
                             "next to this script).")
    parser.add_argument("--window-ms", type=float, default=60.0,
                        help="Search window after release for the first mov/mor update (default 60 ms).")
    parser.add_argument("--stsys-window-ms", type=float, default=200.0,
                        help="Window after release in which system state == 2 is checked (default 200 ms).")
    parser.add_argument("--same-frame-tol-ms", type=float, default=0.2,
                        help="|PB - PA| up to this value is SAME_FRAME (default 0.2 ms = one 200 us sample).")
    parser.add_argument("--offset-suspect-ms", type=float, default=1.0,
                        help="|clock offset| above this value is flagged OFFSET_SUSPECT (default 1.0 ms).")
    parser.add_argument("--signals", default=None,
                        help="JSON signal map (channel names); overrides config/signals.local.json.")
    args = parser.parse_args()
    if args.signals:
        SIG.clear()
        SIG.update(load_signals(args.signals))

    if os.path.isdir(args.target):
        files = sorted({os.path.normcase(f): f for f in
                        glob.glob(os.path.join(args.target, "*.mf4")) +
                        glob.glob(os.path.join(args.target, "*.MF4"))}.values())
        print(f"Batch mode: found {len(files)} .mf4 files in {args.target}")
        default_out = os.path.join(here, "Startup_Order_Analysis.csv")
    else:
        files = [args.target]
        default_out = os.path.join(here, os.path.splitext(os.path.basename(args.target))[0] + "_Startup_Order.csv")
    output = args.output or default_out

    all_rows: List[StartupRow] = []
    for f in files:
        all_rows.extend(analyze_file(f, args))

    if not all_rows:
        print("No start-ups found.")
        return

    df = pd.DataFrame([asdict(r) for r in all_rows])

    print("\n=== Start-up order summary (diff = PATH2 - PATH1) ===")
    print(f"Start-ups analysed          : {len(df)}")
    print(f"Scenarios                   : {df['scenario'].value_counts().to_dict()}")
    print(f"Reset paths                 : {df['reset_paths'].value_counts().to_dict()}")
    print(f"PATH2 release reason        : {df['p2_release_reason'].value_counts().to_dict()}")
    print(f"Order_Verdict               : {df['Order_Verdict'].value_counts().to_dict()}")
    print(f"order_rule                  : {df['order_rule'].value_counts().to_dict()}")
    print(f"Order_Result                : {df['Order_Result'].value_counts().to_dict()}")
    print(f"SysState==2 on PATH1          : {df['p1_stsys2'].value_counts().to_dict()}")
    print(f"SysState==2 on PATH2          : {df['p2_stsys2'].value_counts().to_dict()}")
    print(f"Offset_Flag                 : {df['Offset_Flag'].value_counts().to_dict()}")
    if df['clock_offset_ms'].notna().any():
        print(f"Clock offset [ms]           : min {df['clock_offset_ms'].min():.3f}  "
              f"max {df['clock_offset_ms'].max():.3f}  median {df['clock_offset_ms'].median():.3f}")
    if df['corrected_diff_mor_ms'].notna().any():
        print(f"Corrected PB-PA main-cnt [ms]    : min {df['corrected_diff_mor_ms'].min():.3f}  "
              f"max {df['corrected_diff_mor_ms'].max():.3f}")
    print(f"Old Flag (|raw|>5 ms)       : {df['Flag'].value_counts().to_dict()}")
    print(f"Old Negative_Flag (raw<0)   : {df['Negative_Flag'].value_counts().to_dict()}")

    df.to_csv(output, index=False)
    print(f"\nDetailed results saved to {output}")


if __name__ == "__main__":
    main()
