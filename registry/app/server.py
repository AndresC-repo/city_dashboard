"""
Dataset registry server — Python standard library only.

First start: if REGISTRY_FILE does not exist, SEED_FILE (default /seed/registry.json) is copied, else it starts empty.
State lives in one JSON file on a mounted volume (REGISTRY_FILE, default /data/registry.json). Every save keeps a
timestamped copy in /data/history/ (last HISTORY_KEEP). Optional: DATA_ROOT = the project's data folder mounted
read-only; /api/scan then reports, for each dataset's `path`, whether files exist there, how many, total size and the
newest modification date — so "where have we gotten the data" is checked against the disk, not just typed in.

Endpoints
  GET  /                      the page (static/index.html)
  GET  /api/registry          the registry; header ETag = revision
  PUT  /api/registry          replace the registry; send If-Match: <revision>; 409 if someone saved in between
  GET  /api/scan              file presence per dataset path under DATA_ROOT
  GET  /api/export.md         Markdown data map (for the repo / a paper appendix)
  GET  /api/history           list of saved revisions
All paths can sit under a prefix: BASE_PATH=/registry serves /registry/ and /registry/api/… (for a tab behind the
project app's reverse proxy). Requests without the prefix are accepted too, so a proxy may strip it or not.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REG = Path(os.environ.get("REGISTRY_FILE", "/data/registry.json"))
HIST = REG.parent / "history"
DATA_ROOT = Path(os.environ["DATA_ROOT"]) if os.environ.get("DATA_ROOT") else None
KEEP = int(os.environ.get("HISTORY_KEEP", "50"))
PORT = int(os.environ.get("PORT", "8080"))
BASE = "/" + os.environ.get("BASE_PATH", "").strip("/")      # e.g. /registry when served as a tab under another app
BASE = "" if BASE == "/" else BASE
LOCK = threading.Lock()

EMPTY = {"project": {"title": "Data registry", "description": "", "contact": "",
                     "scope": {"label": "Country", "values": []},
                     "groups": ["Outcomes", "Treatment", "Controls and denominators", "Geography", "Other"],
                     "notes": []},
         "datasets": []}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> dict:
    if not REG.exists():
        REG.parent.mkdir(parents=True, exist_ok=True)
        seed = Path(os.environ.get("SEED_FILE", "/seed/registry.json"))
        doc = json.loads(seed.read_text(encoding="utf-8")) if seed.exists() else dict(EMPTY)
        doc.update(revision=0, saved_at=now())                # first start: copy the seed (or start empty)
        REG.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return json.loads(REG.read_text(encoding="utf-8"))


def save(doc: dict) -> dict:
    HIST.mkdir(parents=True, exist_ok=True)
    if REG.exists():
        old = json.loads(REG.read_text(encoding="utf-8"))
        shutil.copy2(REG, HIST / f"registry_r{old.get('revision', 0):05d}.json")
        for f in sorted(HIST.glob("registry_r*.json"))[:-KEEP]:
            f.unlink()
    tmp = REG.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(REG)                                        # atomic: never a half-written registry
    return doc


def scan(doc: dict) -> dict:
    out = {}
    for d in doc.get("datasets", []):
        p = (d.get("path") or "").strip()
        if DATA_ROOT is None:
            out[d["id"]] = {"state": "no-root"}
            continue
        if not p or p.startswith("("):
            out[d["id"]] = {"state": "no-path"}
            continue
        full = (DATA_ROOT / p).resolve()
        if DATA_ROOT.resolve() not in full.parents and full != DATA_ROOT.resolve():
            out[d["id"]] = {"state": "outside-root"}
            continue
        if not full.exists():
            out[d["id"]] = {"state": "missing"}
            continue
        files = [full] if full.is_file() else [f for f in full.rglob("*") if f.is_file()]
        out[d["id"]] = {"state": "found" if files else "empty", "files": len(files),
                        "bytes": sum(f.stat().st_size for f in files),
                        "newest": max((f.stat().st_mtime for f in files), default=None)}
    return out


def export_md(doc: dict) -> str:
    pr = doc["project"]
    L = [f"# {pr.get('title', 'Data registry')}", "", pr.get("description", ""), "",
         f"Exported {now()} (revision {doc.get('revision', 0)}).", ""]
    for sc in pr.get("scope", {}).get("values", []) or [None]:
        rows = [d for d in doc["datasets"] if sc is None or d.get("scope") == sc]
        if not rows:
            continue
        if sc:
            L += [f"## {sc}", ""]
        L += ["| Dataset | Publisher | Role | Use | Acquisition | Version | Coverage | Folder | Source |",
              "|---|---|---|---|---|---|---|---|---|"]
        for d in rows:
            src = "; ".join(f"[{l['label']}]({l['url']})" for l in d.get("links", []))
            acq = d.get("acquisition", {})
            L.append("| " + " | ".join(str(x).replace("|", "/") for x in [
                d.get("name", ""), d.get("publisher", ""), d.get("group", ""), d.get("use", ""),
                acq.get("status", "") + (f" ({acq['date']})" if acq.get("date") else ""), d.get("version", ""),
                d.get("coverage", ""), d.get("path", ""), src]) + " |")
        L.append("")
    return "\n".join(L)


class H(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj, extra: dict | None = None) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

    def _path(self) -> str:
        path = self.path.split("?")[0]
        if BASE and (path == BASE or path.startswith(BASE + "/")):
            path = path[len(BASE):] or "/"
        return path

    def do_GET(self) -> None:
        if BASE and self.path.split("?")[0] == BASE:                 # /registry → /registry/ (relative links)
            return self._send(301, b"", "text/plain", {"Location": BASE + "/"})
        path = self._path()
        if path in ("/", "/index.html"):
            return self._send(200, (HERE / "static/index.html").read_bytes(), "text/html; charset=utf-8")
        with LOCK:
            doc = load()
        if path == "/api/registry":
            return self._json(200, doc, {"ETag": str(doc.get("revision", 0))})
        if path == "/api/scan":
            return self._json(200, {"data_root": str(DATA_ROOT) if DATA_ROOT else None, "at": now(),
                                    "results": scan(doc)})
        if path == "/api/export.md":
            return self._send(200, export_md(doc).encode(), "text/markdown; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="DATA_MAP_export.md"'})
        if path == "/api/history":
            return self._json(200, sorted(f.name for f in HIST.glob("registry_r*.json")) if HIST.exists() else [])
        self._send(404, b"not found", "text/plain")

    def do_PUT(self) -> None:
        if self._path() != "/api/registry":
            return self._send(404, b"not found", "text/plain")
        n = int(self.headers.get("Content-Length", "0"))
        try:
            new = json.loads(self.rfile.read(n))
            assert isinstance(new.get("datasets"), list) and isinstance(new.get("project"), dict)
            ids = [d.get("id") for d in new["datasets"]]
            assert all(ids) and len(ids) == len(set(ids)), "every dataset needs a unique id"
        except Exception as e:                               # noqa: BLE001
            return self._json(400, {"error": f"invalid registry: {e}"})
        with LOCK:
            cur = load()
            want = self.headers.get("If-Match")
            if want is not None and want != str(cur.get("revision", 0)):
                return self._json(409, {"error": "changed elsewhere", "current": cur},
                                  {"ETag": str(cur.get("revision", 0))})
            new["revision"] = cur.get("revision", 0) + 1
            new["saved_at"] = now()
            save(new)
        self._json(200, {"revision": new["revision"], "saved_at": new["saved_at"]}, {"ETag": str(new["revision"])})

    def log_message(self, fmt: str, *args) -> None:           # one short line per request
        print(f"{time.strftime('%H:%M:%S')} {self.command} {self.path} {args[1] if len(args) > 1 else ''}", flush=True)


if __name__ == "__main__":
    load()
    print(f"registry: {REG} | data root: {DATA_ROOT} | http://0.0.0.0:{PORT}{BASE}/", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
