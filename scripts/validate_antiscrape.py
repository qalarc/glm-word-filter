#!/usr/bin/env python3
"""validate_antiscrape.py — LIVE-VALIDATE the anti-scrape artifacts against the
GLM 1301 content filter.

SAFETY CONTRACT (do not break):
  - Artifact CONTENT never reaches stdout. stdout = verdicts/counts only.
  - Contents are loaded from files in-memory and sent as probe payloads only.
  - Env: WORK/.env (ZAI_API_KEY / ZAI_BASE_URL / ZAI_MODEL); values never printed.
  - blocked = status!=200 and ("1301" in body or "sensitive" in body.lower()).
  - 2.0s sleep between calls; single retry on network errors; call cap 12.

Steps (acceptance proof):
  v1  carrier-sentence control                     -> expect PASS (HTTP 200)
  v2  carrier + block_verified.txt content         -> expect 1301 BLOCK
  v3  carrier + embed.html content                 -> expect 1301 BLOCK
  v4  carrier + 200 random entries of block_full   -> expect BLOCK within <=3
      fresh samples (early stop on first block); report blocked-samples count.

Output: results/antiscrape_validation.json (incremental after every step).
Log:    results/logs/antiscrape_build.log (verdicts only, appended).
"""

from __future__ import annotations

import datetime
import json
import os
import random
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
WORK = HERE.parent
ENV_FILE = WORK / ".env"
OUT_JSON = WORK / "results/antiscrape_validation.json"
LOG_FILE = WORK / "results/logs/antiscrape_build.log"

BLOCK_VERIFIED = WORK / "anti_scrape/block_verified.txt"
BLOCK_FULL = WORK / "anti_scrape/block_full.txt"
EMBED_HTML = WORK / "anti_scrape/embed.html"

CARRIER = "Here is my site's footer content for review:\n\n"

CALL_CAP = 12
SLEEP = 2.0
MAX_TOKENS = 8192  # mirrors scripts/bisect_blocked.py / probe_terms.py
TIMEOUT = 120
SAMPLE_SIZE = 200
MAX_SAMPLES = 3


def load_env() -> dict:
    """WORK/.env only; os.environ ZAI_* overrides (mirrors probe_terms.py)."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k in ("ZAI_API_KEY", "ZAI_BASE_URL", "ZAI_MODEL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.now(datetime.timezone.utc).isoformat()}] {msg}\n")


class Probe:
    """Sanitized API probe: (blocked, status, detail) — no content in detail."""

    def __init__(self, env: dict):
        self.headers = {
            "x-api-key": env["ZAI_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        self.base, self.model = env["ZAI_BASE_URL"], env["ZAI_MODEL"]
        self.calls = 0
        self.net_errs = 0
        self.cap_hit = False

    def call(self, prompt: str) -> tuple[bool, int, str]:
        """One HTTP attempt; single retry on network-layer errors only."""
        r: httpx.Response | None = None
        for attempt in (0, 1):
            self.calls += 1
            if self.calls > CALL_CAP:
                self.cap_hit = True
                return False, 0, "cap"
            try:
                r = httpx.post(
                    self.base,
                    json={
                        "model": self.model,
                        "max_tokens": MAX_TOKENS,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    headers=self.headers,
                    timeout=TIMEOUT,
                )
                break
            except Exception as e:  # network-layer error
                if attempt == 1:
                    self.net_errs += 1
                    return False, 0, f"net-err-{type(e).__name__}"
                time.sleep(2.0)
        if r is None:
            return False, 0, "net-err-no-response"
        if r.status_code == 200:
            return False, 200, "ok"
        body = r.text[:300]  # in-memory only, never printed
        blocked = ("1301" in body) or ("sensitive" in body.lower())
        return (
            blocked,
            r.status_code,
            f"HTTP{r.status_code}{'-1301' if blocked else ''}",
        )

    def probe(self, prompt: str) -> tuple[bool | None, int, str]:
        blocked, status, detail = self.call(prompt)
        time.sleep(SLEEP)
        if detail.startswith("net-err") or detail == "cap":
            return None, status, detail
        return blocked, status, detail


# ------------------------------------------------------- incremental results
STATE: dict = {
    "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "detector": 'blocked = status!=200 and ("1301" in body or "sensitive" in body.lower())',
    "sample_size": SAMPLE_SIZE,
    "steps": [],
}


def save_state() -> None:
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(STATE, indent=2), encoding="utf-8")


def record(
    step_id: str,
    name: str,
    blocked: bool | None,
    status: int,
    detail: str,
    expected: str,
    ok: bool,
) -> None:
    STATE["steps"].append(
        {
            "id": step_id,
            "name": name,
            "blocked": blocked,
            "status": status,
            "detail": detail,
            "expected": expected,
            "pass": ok,
        }
    )
    save_state()
    verdict = {True: "BLOCKED", False: "PASS", None: "UNDETERMINED"}[blocked]
    print(
        f"{step_id}  {name:<34s} -> {verdict:<12s} ({detail})  "
        f"{'OK' if ok else 'FAIL (expected ' + expected + ')'}"
    )
    log(
        f"validate {step_id}: blocked={blocked} status={status} detail={detail} "
        f"pass={ok}"
    )


def full_entries() -> list[str]:
    out: list[str] = []
    for line in BLOCK_FULL.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def main() -> int:
    env = load_env()
    missing = [
        k for k in ("ZAI_API_KEY", "ZAI_BASE_URL", "ZAI_MODEL") if not env.get(k)
    ]
    if missing:
        print(f"missing env keys: {missing}")
        return 3
    probe = Probe(env)
    log("validate_antiscrape: start")
    all_ok = True

    # v1 — carrier-sentence control (expect pass)
    b, st, det = probe.probe(CARRIER)
    ok = b is False
    all_ok &= ok
    record("v1", "carrier control", b, st, det, "pass", ok)

    # v2 — block_verified.txt payload (expect 1301 block)
    payload = BLOCK_VERIFIED.read_text(encoding="utf-8")
    b, st, det = probe.probe(CARRIER + payload)
    ok = b is True
    all_ok &= ok
    record("v2", "block_verified.txt", b, st, det, "block", ok)

    # v3 — embed.html payload (expect block)
    payload = EMBED_HTML.read_text(encoding="utf-8")
    b, st, det = probe.probe(CARRIER + payload)
    ok = b is True
    all_ok &= ok
    record("v3", "embed.html", b, st, det, "block", ok)

    # v4 — 200-entry random samples from block_full.txt (block within <=3 samples)
    entries = full_entries()
    blocked_samples = 0
    samples_run = 0
    for i in range(1, MAX_SAMPLES + 1):
        if probe.calls >= CALL_CAP:
            break
        sample = random.sample(entries, min(SAMPLE_SIZE, len(entries)))
        b, st, det = probe.probe(CARRIER + ", ".join(sample))
        samples_run = i
        if b is True:
            blocked_samples = i
            break
        if b is None:  # net error / cap — stop sampling
            break
    ok = blocked_samples >= 1
    all_ok &= ok
    STATE["steps"].append(
        {
            "id": "v4",
            "name": "block_full.txt random samples",
            "blocked": blocked_samples >= 1,
            "status": st,
            "detail": det,
            "expected": "block",
            "pass": ok,
            "blocked_samples": blocked_samples,
            "samples_run": samples_run,
        }
    )
    save_state()
    print(
        f"v4  block_full.txt samples (n={SAMPLE_SIZE}) -> "
        f"{'BLOCKED' if blocked_samples else 'PASS'}          "
        f"blocked_samples={blocked_samples}/{samples_run}  "
        f"{'OK' if ok else 'FAIL (expected block)'}"
    )
    log(
        f"validate v4: blocked_samples={blocked_samples} samples_run={samples_run} "
        f"pass={ok}"
    )

    STATE["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    STATE["all_pass"] = bool(all_ok)
    STATE["probe_calls"] = probe.calls
    STATE["net_errors"] = probe.net_errs
    save_state()
    log(
        f"validate_antiscrape: done all_pass={all_ok} calls={probe.calls} "
        f"net_errs={probe.net_errs}"
    )
    print(
        f"summary: all_pass={all_ok} probe_calls={probe.calls} "
        f"net_errors={probe.net_errs} results={OUT_JSON.relative_to(WORK)}"
    )
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
