#!/usr/bin/env python3
"""bisect_blocked.py — mechanically isolate the exact substrings that trip the
GLM 1301 content filter on two known-blocked classification prompts.

SAFETY CONTRACT (do not break):
  - Thread/vocab content NEVER reaches stdout. stdout = counts/ids/verdicts only.
  - Isolated trigger substrings are written ONLY to vocab/isolated_triggers_<ts>.json.
  - The action log (stdout, tee'd by caller to results/logs/bisect.log) has no content.
  - Env override order mirrors ops/filter_probe.py: os.environ GLM_* beats engine/.env.
  - ~1.2s sleep between API calls; single retry on network errors; 150-call cap/thread.
  - Partial results are always written (per-thread try/except + write-after-each-thread).

Run:
  GLM_API_KEY=<dedicated key> engine/.venv/bin/python3 scripts/bisect_blocked.py [--selftest]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
WORK = HERE.parent
ENGINE = Path.home() / "projects/MASTER_PROJECTS/chanalyse/engine"
sys.path.insert(0, str(ENGINE))

DB = Path.home() / ".cache/chanalyse/gmktec.db"
KNOWN_HARD = [136080, 138356]
CALL_CAP = 150  # max API calls per thread
SLEEP = 1.2  # seconds between API calls
MIN_PIECE = 60  # stop bisecting below this length
WIN_MIN, WIN_MAX = 200, 400
WRAPPER = "Please review the following excerpt and reply with the single word DONE.\n\n"


def load_env() -> dict:
    """engine/.env defaults, overridden by os.environ GLM_* (test-key support)."""
    env = {}
    p = ENGINE / ".env"
    if p.exists():
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k in ("GLM_API_KEY", "GLM_BASE_URL", "GLM_MODEL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


class Probe:
    """Sanitized API probe: (blocked, detail) — detail never contains content."""

    def __init__(self, env: dict):
        self.headers = {
            "x-api-key": env["GLM_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        self.base, self.model = env["GLM_BASE_URL"], env["GLM_MODEL"]
        self.calls = 0
        self.net_errs = 0
        self.cap_hit = False

    def call(self, prompt: str) -> tuple[bool, str]:
        """One HTTP attempt; single retry on network errors only."""
        r: httpx.Response | None = None
        for attempt in (0, 1):
            self.calls += 1
            try:
                r = httpx.post(
                    self.base,
                    json={
                        "model": self.model,
                        "max_tokens": 8192,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    headers=self.headers,
                    timeout=120,
                )
                break
            except Exception as e:  # network-layer error
                if attempt == 1:
                    self.net_errs += 1
                    return False, f"net-err-{type(e).__name__}"
                time.sleep(2.0)
        if r is None:  # unreachable, but keeps the type checker honest
            return False, "net-err-no-response"
        if r.status_code == 200:
            return False, "ok"
        body = r.text[:300]  # inspected in-memory only, never returned/printed
        blocked = ("1301" in body) or ("sensitive" in body.lower())
        return blocked, f"HTTP{r.status_code}{'-1301' if blocked else ''}"

    def probe(self, text: str, raw: bool = False) -> tuple[bool | None, str]:
        """None verdict => could not test (cap hit / persistent net error)."""
        if self.calls >= CALL_CAP:
            self.cap_hit = True
            return None, "cap"
        blocked, detail = self.call(text if raw else WRAPPER + text)
        time.sleep(SLEEP)
        if detail.startswith("net-err"):
            return None, detail
        return blocked, detail


# ────────────────────────────────────────────────────────────────────
# Windowing: exact-reconstruction sentence-ish windows, 200–400 chars
# ────────────────────────────────────────────────────────────────────


def split_units(text: str) -> list[str]:
    """Split on newlines + sentence boundaries. "".join(units) == text exactly."""
    units: list[str] = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        nl = "\n" if i < len(lines) - 1 else ""
        if line == "":
            if nl:
                units.append(nl)
            continue
        sents = re.findall(r"[^.!?]*[.!?]+[\s]*|[^.!?]+", line)
        if not sents:
            sents = [line]
        sents[-1] += nl
        units.extend(s for s in sents if s != "")
    return units


def hard_split(unit: str, maxlen: int) -> list[str]:
    """Word-boundary chunks, each <= maxlen; reconstruction exact."""
    pieces: list[str] = []
    while len(unit) > maxlen:
        cut = unit.rfind(" ", 0, maxlen)
        if cut <= 0:
            pieces.append(unit[:maxlen])
            unit = unit[maxlen:]
        else:
            pieces.append(unit[: cut + 1])
            unit = unit[cut + 1 :]
    if unit:
        pieces.append(unit)
    return pieces


def pack(units: list[str], maxlen: int) -> list[str]:
    """Greedy pack into windows of <= maxlen (typically >= WIN_MIN)."""
    windows: list[str] = []
    cur = ""
    for u in units:
        for piece in hard_split(u, maxlen):
            if cur and len(cur) + len(piece) > maxlen:
                windows.append(cur)
                cur = ""
            cur += piece
    if cur:
        windows.append(cur)
    return windows


def make_windows(prompt: str) -> list[str]:
    """Header/instructions (up to the THREADS: marker) become their own window(s);
    body and trailing instructions are windowed separately. Order preserved."""
    marker = "THREADS:\n"
    idx = prompt.find(marker)
    if idx != -1:
        head_end = idx + len(marker)
        header, rest = prompt[:head_end], prompt[head_end:]
    else:
        header, rest = "", prompt
    fidx = rest.rfind("\nREMINDER:")
    if fidx != -1:
        footer = rest[fidx + 1 :]
        rest = rest[: fidx + 1]
    else:
        footer = ""
    windows = (
        pack(split_units(header), WIN_MAX)
        + pack(split_units(rest), WIN_MAX)
        + pack(split_units(footer), WIN_MAX)
    )
    if "".join(windows) != prompt:  # safety net: never lose a byte
        windows = pack(split_units(prompt), WIN_MAX)
    if "".join(windows) != prompt:  # last resort: fixed word-boundary chunks
        windows = pack([" ".join(prompt.split(" "))], WIN_MAX)
    return windows


# ────────────────────────────────────────────────────────────────────
# Bisection of a known-blocking window down to minimal pieces
# ────────────────────────────────────────────────────────────────────


def split_at_word(piece: str) -> tuple[str, str]:
    """Split at the word boundary nearest the midpoint (separator space dropped)."""
    mid = len(piece) // 2
    li = piece.rfind(" ", 0, mid)
    ri = piece.find(" ", mid)
    if li == -1 and ri == -1:
        return piece[:mid], piece[mid:]
    cut = li if ri == -1 or (li != -1 and (mid - li) <= (ri - mid)) else ri
    return piece[:cut], piece[cut + 1 :]


def bisect(pr: Probe, piece: str, pieces: list[str], state: dict) -> None:
    """piece is known-blocking. Recurse on blocking halves until < MIN_PIECE."""
    if pr.cap_hit:
        return
    if len(piece) < MIN_PIECE:
        pieces.append(piece)
        return
    a, b = split_at_word(piece)
    results = []
    for half in (a, b):
        if pr.calls >= CALL_CAP:
            pr.cap_hit = True
            break
        if not half.strip():
            results.append("skip")
            continue
        blk, _det = pr.probe(half)
        results.append(blk)
        if blk:
            bisect(pr, half, pieces, state)
    if results and all(r is False for r in results):
        state["resistant"] += 1  # window blocks but neither half does


# ────────────────────────────────────────────────────────────────────
# Per-thread pipeline
# ────────────────────────────────────────────────────────────────────


def build_prompt_for(tid: int) -> str:
    from chanalyse.classify.base import ThreadForClassify, build_prompt

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    r = db.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()
    if r is None:
        raise LookupError(f"thread {tid} not found")
    t = ThreadForClassify(
        id=r["id"],
        board=r["board"],
        thread_no=r["thread_no"] or 0,
        subject=(r["original_subject"] or "")[:200],
        comment=(r["original_comment"] or r["cleaned_text"] or "")[:600],
        replies=r["replies"] or 0,
    )
    db.close()
    return build_prompt([t])


def process(tid: int) -> tuple[dict, Probe]:
    from chanalyse.classify.base import ThreadForClassify  # noqa: F401 (path check)

    prompt = build_prompt_for(tid)
    pr = Probe(load_env())
    entry: dict = {"id": tid}

    # b. sanity: exact original prompt must block
    c0, c0_detail = pr.call(prompt)
    time.sleep(SLEEP)
    entry["c0_blocked"] = bool(c0)
    entry["c0_detail"] = c0_detail
    if not c0:
        entry["c0"] = "pass-now"
        entry["verdict"] = "pass-now"
        return entry, pr

    # c. windows
    windows = make_windows(prompt)
    entry["windows_total"] = len(windows)

    # d. probe each window
    blocking: list[int] = []
    wdetails = []
    for i, w in enumerate(windows):
        if pr.calls >= CALL_CAP:
            pr.cap_hit = True
            break
        blk, det = pr.probe(w)
        wdetails.append({"i": i, "blocked": blk, "detail": det})
        if blk:
            blocking.append(i)
    entry["blocking_windows"] = blocking
    entry["window_details"] = wdetails

    # e. recursive bisection of blocking windows
    pieces: list[str] = []
    state = {"resistant": 0}
    for i in blocking:
        bisect(pr, windows[i], pieces, state)
        if pr.cap_hit:
            break
    entry["isolated_minimal"] = sorted(set(pieces), key=len)
    entry["resistant_windows"] = state["resistant"]

    # f. complement test (and combination/semantic tiebreak if no blocking window)
    combo_pass = False
    if blocking:
        comp = "".join(w for i, w in enumerate(windows) if i not in set(blocking))
        cblk, cdet = pr.probe(comp, raw=True)
        entry["complement_blocked"] = None if cblk is None else bool(cblk)
        entry["complement_detail"] = cdet
    else:
        entry["complement_blocked"] = None
        loo = {}
        for i in range(min(len(windows), 8)):
            if pr.calls >= CALL_CAP:
                pr.cap_hit = True
                break
            comp = "".join(w for j, w in enumerate(windows) if j != i)
            blk, det = pr.probe(comp, raw=True)
            loo[str(i)] = blk
            if blk is False:
                combo_pass = True
        entry["leave_one_out"] = loo

    entry["cap_hit"] = pr.cap_hit
    entry["net_errors"] = pr.net_errs

    # g. verdict
    if blocking:
        if entry["complement_blocked"] is False:
            entry["verdict"] = "wordlist"
        else:  # complement still blocks (or unknown due to cap)
            entry["verdict"] = "combination"
    else:
        entry["verdict"] = (
            "combination" if (combo_pass and not pr.cap_hit) else "semantic"
        )
    return entry, pr


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = WORK / "vocab" / f"isolated_triggers_{ts}.json"
    meta_path = WORK / "results" / "logs" / "bisect_meta.json"

    if args.selftest:
        from chanalyse.classify.base import ThreadForClassify, build_prompt

        fake = ThreadForClassify(
            id=999999,
            board="g",
            thread_no=12345,
            subject="Lorem ipsum dolor sit amet. Consectetur adipiscing elit? " * 4,
            comment=(
                "Sed do eiusmod tempor incididunt ut labore. "
                "Ut enim ad minim veniam, quis nostrud! " * 12
            )[:600],
            replies=12,
        )
        prompt = build_prompt([fake])
        windows = make_windows(prompt)
        ok = "".join(windows) == prompt
        sizes_ok = all(len(w) <= WIN_MAX for w in windows)
        print(
            f"selftest reconstruct={'OK' if ok else 'FAIL'} "
            f"sizes={'OK' if sizes_ok else 'FAIL'} windows={len(windows)} "
            f"prompt_len={len(prompt)}"
        )
        return

    env = load_env()
    results = {"ts": ts, "model": env.get("GLM_MODEL", ""), "results": []}
    meta: dict = {"file": str(out), "per_thread": []}

    try:
        for tid in KNOWN_HARD:
            pr = None
            try:
                entry, pr = process(tid)
            except Exception as e:
                entry = {"id": tid, "error": type(e).__name__}
            results["results"].append(entry)
            out.write_text(json.dumps(results, ensure_ascii=False, indent=1))

            if entry.get("verdict") == "pass-now":
                print(f"id={tid} c0=PASS verdict=skip")
            elif "error" in entry:
                print(f"id={tid} c0=ERR verdict=error")
            else:
                comp = entry.get("complement_blocked")
                cs = "NA" if comp is None else ("BLK" if comp else "pass")
                print(
                    f"id={tid} c0=BLK windows={entry['windows_total']} "
                    f"blocking_windows={len(entry['blocking_windows'])} "
                    f"isolated={len(entry['isolated_minimal'])} "
                    f"complement={cs} verdict={entry['verdict']}"
                )
            meta["per_thread"].append(
                {
                    "id": tid,
                    "calls": pr.calls if pr else 0,
                    "net_errors": pr.net_errs if pr else 0,
                    "cap_hit": bool(pr and pr.cap_hit),
                }
            )
    finally:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=1))
        meta["total_calls"] = sum(p["calls"] for p in meta["per_thread"])
        meta["wall_s"] = round(time.time() - t0, 1)
        meta_path.write_text(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
