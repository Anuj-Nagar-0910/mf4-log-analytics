"""
MF4 Start-up Order Viewer - local GUI backend for analyze_mf4_startup_order.py.

Run:  python server.py   (opens http://127.0.0.1:8766 in your browser)

- Reuses analyze_mf4_startup_order.analyze_file(), so numbers are identical to the CLI script.
- Binds to 127.0.0.1 only (nothing is exposed on the network).
- Files are read in place from disk (no copy). Drag & drop upload is also available
  and saves a temporary copy under %TEMP%\\mf4_startup_order_uploads.
- CSV export is produced with pandas exactly like the CLI (same header, order and number format).
- Result cache: each analysed recording is stored under %LOCALAPPDATA%\\mf4_startup_order_cache. Reopening the same
  recording with the same settings is instant. The cache entry is invalid as soon as the MF4 (size / modification time),
  the settings, the signal map or analyze_mf4_startup_order.py change.
  Optional age limit (UI setting or MF4_GUI_CACHE_MAX_DAYS, 0 = unlimited): older entries are deleted and re-analysed.
- Author: shown in the tool and in every report (MF4_GUI_AUTHOR environment variable, default: Windows user name).
"""
import argparse
import contextlib
import getpass
import hashlib
import io
import json
import math
import os
import socket
import string
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from dataclasses import asdict, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # import the analysis script from mf4_helper/

import analyze_mf4_startup_order as core  # noqa: E402

UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "mf4_startup_order_uploads")
MF4_EXTS = (".mf4", ".mdf")
DEFAULT_START = os.environ.get("MF4_GUI_START", "")  # optional: folder the file browser opens in
COLUMNS = [f.name for f in fields(core.StartupRow)]
DEFAULT_SETTINGS = {"window_ms": 60.0, "stsys_window_ms": 200.0, "same_frame_tol_ms": 0.2, "offset_suspect_ms": 1.0}


def _default_author():
    try:
        return getpass.getuser()
    except Exception:
        return os.environ.get("USERNAME", "unknown")


AUTHOR = os.environ.get("MF4_GUI_AUTHOR") or _default_author()

CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "mf4_startup_order_cache")
CACHE_FORMAT = 1
try:  # optional age limit in days; 0 = unlimited (entries stay valid until file / settings / script change)
    DEFAULT_CACHE_MAX_DAYS = max(0.0, float(os.environ.get("MF4_GUI_CACHE_MAX_DAYS", "0")))
except ValueError:
    DEFAULT_CACHE_MAX_DAYS = 0.0
with open(core.__file__, "rb") as _fh:  # any change of the analysis algorithm invalidates all cache entries
    SCRIPT_HASH = hashlib.sha1(_fh.read()).hexdigest()[:12]


def cache_key(path, settings):
    st = os.stat(path)
    ident = json.dumps({"p": os.path.normcase(os.path.abspath(path)), "size": st.st_size, "mtime": st.st_mtime,
                        "settings": settings, "signals": core.SIG, "script": SCRIPT_HASH, "fmt": CACHE_FORMAT}, sort_keys=True)
    return hashlib.sha1(ident.encode("utf-8")).hexdigest()


def _too_old(created, max_age_days):
    return max_age_days > 0 and (time.time() - created) > max_age_days * 86400.0


def cache_load(key, max_age_days=0.0):
    """Returns (entry or None, expired_flag). An entry older than max_age_days is deleted and not reused."""
    fn = os.path.join(CACHE_DIR, key + ".json")
    if not os.path.isfile(fn):
        return None, False
    try:
        with open(fn, "r", encoding="utf-8") as fh:
            entry = json.load(fh)       # NaN round-trips (json allow_nan) -> CSV export stays identical
    except Exception:
        return None, False              # corrupt entry -> analyse again
    if _too_old(entry.get("created", os.path.getmtime(fn)), max_age_days):
        try:
            os.remove(fn)
        except OSError:
            pass
        return None, True
    return entry, False


def cache_prune(max_age_days, keep=()):
    """Delete all entries older than the age limit (no-op when the limit is 0 = unlimited).
    `keep`: cache keys used / written by the current job - never deleted by that job's clean-up."""
    removed = 0
    if max_age_days <= 0 or not os.path.isdir(CACHE_DIR):
        return 0
    for e in os.scandir(CACHE_DIR):
        if not e.name.endswith(".json") or e.name[:-5] in keep:
            continue
        try:
            with open(e.path, "r", encoding="utf-8") as fh:
                created = json.load(fh).get("created", e.stat().st_mtime)
        except Exception:
            created = e.stat().st_mtime
        if _too_old(created, max_age_days):
            try:
                os.remove(e.path)
                removed += 1
            except OSError:
                pass
    return removed


def cache_save(key, entry):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = os.path.join(CACHE_DIR, key + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entry, fh)
        os.replace(tmp, os.path.join(CACHE_DIR, key + ".json"))
    except OSError:
        traceback.print_exc()           # cache is optional; never fail the analysis because of it


def cache_info():
    n, size, oldest = 0, 0, None
    if os.path.isdir(CACHE_DIR):
        for e in os.scandir(CACHE_DIR):
            if e.name.endswith(".json"):
                n += 1
                size += e.stat().st_size
                mt = e.stat().st_mtime          # entry is written once at creation -> mtime = created
                oldest = mt if oldest is None else min(oldest, mt)
    return {"dir": CACHE_DIR, "entries": n, "size": size, "oldest": oldest}


def cache_clear():
    removed = 0
    if os.path.isdir(CACHE_DIR):
        for e in os.scandir(CACHE_DIR):
            if e.name.endswith((".json", ".tmp")):
                os.remove(e.path)
                removed += 1
    return removed

JOBS = {}
# analyze_file() prints to stdout and we capture that per file, so run one analysis at a time.
ANALYSIS_LOCK = threading.Lock()


def is_mf4(name):
    return name.lower().endswith(MF4_EXTS)


def file_info(path):
    st = os.stat(path)
    return {"name": os.path.basename(path), "path": path, "size": st.st_size, "mtime": st.st_mtime}


def clean(v):
    """JSON-safe value (NaN/inf -> None, numpy -> python)."""
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if hasattr(v, "item"):
        return clean(v.item())
    return v


def list_roots():
    roots = []
    home = os.path.expanduser("~")
    if os.path.isdir(DEFAULT_START):
        roots.append({"name": "Start folder", "path": DEFAULT_START})
    roots.append({"name": "Home", "path": home})
    if os.path.isdir(UPLOAD_DIR):
        roots.append({"name": "Uploads", "path": UPLOAD_DIR})
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if os.path.exists(drive):
            roots.append({"name": drive, "path": drive})
    return roots


def browse(path):
    if not path:
        path = DEFAULT_START if os.path.isdir(DEFAULT_START) else os.path.expanduser("~")
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise ValueError(f"Not a folder: {path}")
    dirs, files = [], []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir():
                        dirs.append({"name": entry.name, "path": entry.path})
                    elif is_mf4(entry.name):
                        st = entry.stat()
                        files.append({"name": entry.name, "path": entry.path,
                                      "size": st.st_size, "mtime": st.st_mtime})
                except OSError:
                    continue
    except PermissionError:
        raise ValueError(f"Permission denied: {path}")
    dirs.sort(key=lambda d: d["name"].lower())
    files.sort(key=lambda f: f["name"].lower())
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else "", "dirs": dirs, "files": files}


def resolve_paths(paths):
    """Expand pasted paths: files stay, folders expand to the MF4 files directly inside them."""
    found, errors = [], []
    for raw in paths:
        p = raw.strip().strip('"').strip("'")
        if not p:
            continue
        if os.path.isdir(p):
            names = sorted(n for n in os.listdir(p) if is_mf4(n))
            if not names:
                errors.append(f"No .mf4 files in folder: {p}")
            for n in names:
                found.append(file_info(os.path.join(p, n)))
        elif os.path.isfile(p):
            found.append(file_info(p))
        else:
            errors.append(f"Not found: {p}")
    return {"files": found, "errors": errors}


def run_job(job_id):
    job = JOBS[job_id]
    args = SimpleNamespace(**job["settings"])
    job_keys = set()
    for f in job["files"]:
        f["status"] = "running"
        t0 = time.time()
        buf = io.StringIO()
        try:
            key = cache_key(f["path"], job["settings"])
            job_keys.add(key)
            hit, expired = cache_load(key, job["cache_max_days"]) if job["use_cache"] else (None, False)
            expired_note = (f"cache entry older than {job['cache_max_days']:g} day(s) → re-analysed"
                            if expired else "")
            if hit is not None:
                rows = [core.StartupRow(**d) for d in hit["rows"]]
                timelines = hit["timelines"]
                warns, other = hit.get("warnings", []), hit.get("other", [])
                f["cached"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(hit.get("created", 0)))
            else:
                timelines = {}
                with ANALYSIS_LOCK, contextlib.redirect_stdout(buf):
                    rows = core.analyze_file(f["path"], args, timelines)
                lines = [ln.strip() for ln in buf.getvalue().splitlines()
                         if ln.strip() and not ln.startswith("Analyzing ") and "start-up(s) found" not in ln]
                warns = [ln for ln in lines if ln.startswith("[warn]")]
                other = [ln for ln in lines if not ln.startswith("[warn]")]
                if rows or other:   # skipped files are cached as well (with their reason)
                    cache_save(key, {"rows": [asdict(r) for r in rows], "timelines": timelines, "warnings": warns,
                                     "other": other, "created": time.time(), "file": f["path"],
                                     "settings": job["settings"], "script": SCRIPT_HASH, "author": AUTHOR})
                f["cached"] = ""
            if rows:
                f["status"] = "done"
                f["message"] = f"{len(warns)} channel warning(s)" if warns else ""
            else:
                f["status"] = "skipped"
                f["message"] = " ".join(other) or "No start-ups found."
            if expired_note:
                f["message"] = f"{f['message']} · {expired_note}" if f["message"] else expired_note
            f["warnings"] = warns
            for r in rows:
                job["_rowobjs"].append(r)
                d = {k: clean(v) for k, v in asdict(r).items()}
                d["key"] = f"{r.file_name}#{r.startup_idx}"
                job["rows"].append(d)
            job["timelines"].update(timelines)
            f["count"] = len(rows)
        except Exception as exc:  # keep the batch going if one file fails
            traceback.print_exc()
            f["status"] = "error"
            f["message"] = f"{type(exc).__name__}: {exc}"
            f["count"] = 0
        f["elapsed"] = round(time.time() - t0, 1)
    if job["use_cache"]:
        # after the run (so expired entries of this job's files were reported above): clean up all other old entries
        job["cache_pruned"] = cache_prune(job["cache_max_days"], keep=job_keys)
    job["state"] = "done"
    job["finished"] = time.time()


def start_job(paths, settings, use_cache=True, cache_max_days=None):
    files = []
    seen = set()
    for p in paths:
        p = p.strip().strip('"')
        if os.path.isfile(p) and os.path.normcase(p) not in seen:
            seen.add(os.path.normcase(p))
            files.append({"name": os.path.basename(p), "path": p, "status": "queued",
                          "message": "", "count": 0, "elapsed": 0, "warnings": [], "cached": ""})
    if not files:
        raise ValueError("No valid files to analyze.")
    s = dict(DEFAULT_SETTINGS)
    for k in s:
        try:
            if settings and settings.get(k) not in (None, ""):
                s[k] = float(settings[k])
        except (TypeError, ValueError):
            raise ValueError(f"Invalid setting {k}: {settings.get(k)}")
    try:
        max_days = DEFAULT_CACHE_MAX_DAYS if cache_max_days in (None, "") else float(cache_max_days)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid cache max age: {cache_max_days}")
    if max_days < 0:
        raise ValueError("Cache max age must be >= 0 (0 = unlimited).")
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"cache_max_days": max_days,"id": job_id, "state": "running", "files": files, "rows": [], "timelines": {},
                    "_rowobjs": [], "settings": s, "columns": COLUMNS, "use_cache": bool(use_cache),
                    "author": AUTHOR, "script": SCRIPT_HASH, "started": time.time(), "finished": None}
    threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    return job_id


def export_csv(job_id, keys):
    job = JOBS.get(job_id)
    if not job:
        raise ValueError("Unknown job")
    objs = job["_rowobjs"]
    if keys:
        wanted = set(keys)
        objs = [r for r in objs if f"{r.file_name}#{r.startup_idx}" in wanted]
    df = pd.DataFrame([asdict(r) for r in objs], columns=COLUMNS)
    # identical to the CLI file (df.to_csv(output, index=False) writes os.linesep line endings)
    return df.to_csv(index=False, lineterminator=os.linesep)


class Handler(BaseHTTPRequestHandler):
    server_version = "MF4StartupOrder/1.0"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, body, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send(json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8", status)

    def _body_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    self._send(fh.read(), "text/html; charset=utf-8")
            elif url.path == "/api/roots":
                self._json({"roots": list_roots(), "start": DEFAULT_START if os.path.isdir(DEFAULT_START) else "",
                            "defaults": DEFAULT_SETTINGS, "author": AUTHOR, "cache": cache_info(),
                            "cache_max_days": DEFAULT_CACHE_MAX_DAYS})
            elif url.path == "/api/cache":
                self._json(cache_info())
            elif url.path == "/api/browse":
                self._json(browse(q.get("path", [""])[0]))
            elif url.path == "/api/job":
                job = JOBS.get(q.get("id", [""])[0])
                if not job:
                    self._json({"error": "Unknown job"}, 404)
                else:
                    self._json({k: v for k, v in job.items() if not k.startswith("_")})
            else:
                self._json({"error": "Not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self):
        url = urlparse(self.path)
        try:
            if url.path == "/api/analyze":
                data = self._body_json()
                self._json({"job": start_job(data.get("paths", []), data.get("settings", {}),
                                             data.get("use_cache", True), data.get("cache_max_days"))})
            elif url.path == "/api/cache_clear":
                self._json({"removed": cache_clear(), **cache_info()})
            elif url.path == "/api/resolve":
                self._json(resolve_paths(self._body_json().get("paths", [])))
            elif url.path == "/api/export_csv":
                data = self._body_json()
                body = export_csv(data.get("job", ""), data.get("keys") or []).encode("utf-8")
                self._send(body, "text/csv; charset=utf-8")
            elif url.path == "/api/upload":
                name = os.path.basename(parse_qs(url.query).get("name", ["upload.mf4"])[0])
                if not is_mf4(name):
                    raise ValueError("Only .mf4 / .mdf files can be uploaded.")
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                dest = os.path.join(UPLOAD_DIR, name)
                remaining = int(self.headers.get("Content-Length", 0))
                with open(dest, "wb") as out:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        remaining -= len(chunk)
                self._json(file_info(dest))
            else:
                self._json({"error": "Not found"}, 404)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            traceback.print_exc()
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


class ExclusiveServer(ThreadingHTTPServer):
    """On Windows SO_REUSEADDR lets a second instance bind a port that is already in use, so a new viewer could
    silently share the port with a running one. Bind exclusively; a busy port then fails and the next one is tried."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    ap = argparse.ArgumentParser(description="MF4 Start-up Order Viewer")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    httpd = None
    for port in range(args.port, args.port + 11):
        try:
            httpd = ExclusiveServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if httpd is None:
        sys.exit("Could not find a free port.")

    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"MF4 Start-up Order Viewer running at {url}   (Ctrl+C to stop)", flush=True)
    print(f"Author: {AUTHOR}   Cache: {CACHE_DIR}", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
