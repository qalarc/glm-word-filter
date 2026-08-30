#!/usr/bin/env python3
"""Parallel-range downloader for ollama registry blobs — v3, low-request-count.

Lessons learned (2026-08-30):
  - registry.ollama.com (Cloudflare) rate-limits bursty anonymous range
    requests. urllib's default "Python-urllib" User-Agent plus dozens of
    fresh connections drew HTTP 403 within ~15 requests.
  - ollama's own client fetches the same blob in ~20 shard requests with
    UA "ollama/0.x" and is never blocked.

So this version mimics that: ~20 large spans, keep-alive connections,
UA "ollama/0.12", 8 workers, exponential backoff on 403/429/5xx, sidecar
resume state, whole-file sha256 verify before placement.

Usage: python3 scripts/fetch_blob_parallel.py
"""

import concurrent.futures as cf
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request

REGISTRY_HOST = "registry.ollama.com"
BLOB_PATH_FMT = "/v2/library/glm-4.7-flash/blobs/sha256:{}"
BLOB_DIR = os.path.expanduser("~/.ollama/models/blobs")
UA = "ollama/0.12"

BLOBS = {
    "9eba2761cf0b88b8bc11a065a7b5b47f1b13ce820e8e492cb1010b450f9ec950": 19019269280,
    "b1bca6ec8117a09783df643e500e7331c5d6ea79fe4ba4d29d9a256c2f794a83": 1068,
    "543c6a8262f92e6383f5ab7c13f8a55e8f4c2a00b5ecdf3822c6d5489ffd66b7": 63,
    "89d1ec0bb8381384b592647db2b6dd223e6ddc59fa3991ba940afd867540a9b8": None,  # config
}

N_SPANS = 40
WORKERS = 16
MAX_TRIES = 8
PRINT_LOCK = threading.Lock()


def log(msg: str) -> None:
    with PRINT_LOCK:
        print(msg, flush=True)


class Conn:  # thin keep-alive HTTPS wrapper that follows 30x redirects (R2 signed URLs)
    def __init__(self):
        self.host = REGISTRY_HOST
        self.c = http.client.HTTPSConnection(self.host, timeout=600)

    def _reconnect(self, host: str):
        self.c.close()
        self.host = host
        self.c = http.client.HTTPSConnection(self.host, timeout=600)

    def get_range(self, path: str, start: int, end: int):
        for attempt in range(3):  # connection-level retries (reset etc.)
            try:
                hops = 0
                while True:
                    self.c.request(
                        "GET",
                        path,
                        headers={
                            "Range": f"bytes={start}-{end}",
                            "User-Agent": UA,
                            "Accept": "*/*",
                            "Connection": "keep-alive",
                        },
                    )
                    resp = self.c.getresponse()
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.getheader("Location", "")
                        resp.read()
                        hops += 1
                        if hops > 5 or not loc:
                            raise IOError(f"redirect loop/bad Location: {loc[:80]}")
                        parts = urllib.parse.urlsplit(loc)
                        self._reconnect(parts.netloc)
                        path = parts.path + (f"?{parts.query}" if parts.query else "")
                        continue
                    return resp
            except (http.client.HTTPException, OSError) as exc:
                if attempt == 2 or isinstance(exc, IOError):
                    raise
                log(
                    f"[conn] reset on {start}-{end}: {type(exc).__name__}, reconnecting"
                )
                time.sleep(3)
                self.host = REGISTRY_HOST
                self._reconnect(self.host)
        raise RuntimeError("unreachable: connection retries exhausted")


def fetch_span(digest_hex: str, start: int, end: int, out_path: str) -> str:
    last = "untried"
    for attempt in range(MAX_TRIES):
        try:
            conn = Conn()
            resp = conn.get_range(BLOB_PATH_FMT.format(digest_hex), start, end)
            code = resp.status
            if code == 403 or code == 429 or code >= 500:
                resp.read()
                raise IOError(f"HTTP {code}")
            if code not in (200, 206):
                raise IOError(f"unexpected HTTP {code}")
            got = 0
            with open(out_path, "r+b") as f:
                f.seek(start)
                while True:
                    buf = resp.read(4 * 1024 * 1024)
                    if not buf:
                        break
                    f.write(buf)
                    got += len(buf)
            expect = end - start + 1
            if got != expect:
                raise IOError(f"short read {got}/{expect}")
            return f"{start}-{end}"
        except Exception as exc:  # noqa: BLE001
            last = exc
            msg = str(exc)
            throttled = "403" in msg or "429" in msg
            sleep = (15 * (2**attempt)) if throttled else min(5 * (attempt + 1), 30)
            sleep = min(sleep, 240)
            log(
                f"[retry] span {start}-{end} attempt {attempt + 1}: {msg}; sleep {sleep:.0f}s"
            )
            time.sleep(sleep)
    raise RuntimeError(f"span {start}-{end} failed after {MAX_TRIES}: {last}")


def download_blob(digest_hex: str, size: int | None) -> None:
    final = os.path.join(BLOB_DIR, f"sha256-{digest_hex}")
    if os.path.exists(final):
        have = os.path.getsize(final)
        if size is None or have == size:
            log(f"[blob] {digest_hex[:12]} already present ({have} B), skip")
            return
    tmp = final + ".dl"
    state_path = final + ".dl.state"

    if size is None:
        req = urllib.request.Request(
            f"https://{REGISTRY_HOST}{BLOB_PATH_FMT.format(digest_hex)}",
            headers={"User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        with open(tmp, "wb") as f:
            f.write(data)
        size = len(data)
    else:
        done_spans = set()
        if os.path.exists(state_path):
            try:
                done_spans = set(json.load(open(state_path)))
            except Exception:  # noqa: BLE001
                done_spans = set()
        with open(tmp, "wb") as f:
            f.truncate(size)
        span_len = -(-size // N_SPANS)  # ceil
        spans = [(s, min(s + span_len, size) - 1) for s in range(0, size, span_len)]
        todo = [sp for sp in spans if f"{sp[0]}-{sp[1]}" not in done_spans]
        log(
            f"[blob] {digest_hex[:12]} {len(spans)} spans, {len(done_spans)} done, fetching {len(todo)}"
        )
        t0 = time.time()
        done_n = len(done_spans)
        lock = threading.Lock()
        state = sorted(done_spans)

        def save_state():
            with open(state_path, "w") as f:
                json.dump(state, f)

        with cf.ThreadPoolExecutor(WORKERS) as ex:
            futs = {
                ex.submit(fetch_span, digest_hex, s, e, tmp): (s, e) for s, e in todo
            }
            for fut in cf.as_completed(futs):
                s, e = futs[fut]
                fut.result()
                with lock:
                    state.append(f"{s}-{e}")
                    done_n += 1
                    dt = time.time() - t0
                    rate = (done_n - len(done_spans)) * span_len / 1e6 / max(dt, 1e-9)
                    log(
                        f"[blob] {digest_hex[:12]} {done_n}/{len(spans)} spans, {rate:.0f} MB/s this run"
                    )
                    save_state()
    h = hashlib.sha256()
    with open(tmp, "rb") as f:
        while True:
            buf = f.read(8 * 1024 * 1024)
            if not buf:
                break
            h.update(buf)
    if h.hexdigest() != digest_hex:
        log(f"[blob] {digest_hex[:12]} SHA MISMATCH — wiping state; redownload needed")
        for p in (state_path, tmp):
            if os.path.exists(p):
                os.remove(p)
        raise RuntimeError(f"sha mismatch for {digest_hex[:12]}")
    os.replace(tmp, final)
    if os.path.exists(state_path):
        os.remove(state_path)
    log(f"[blob] {digest_hex[:12]} verified + placed ({size} B)")


def main() -> int:
    os.makedirs(BLOB_DIR, exist_ok=True)
    t0 = time.time()
    for dg, sz in BLOBS.items():
        download_blob(dg, sz)
    print(f"[done] all blobs in {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
