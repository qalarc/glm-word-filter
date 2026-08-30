#!/usr/bin/env python3
"""acceptance_rescue.py — prove a swap map built from VERIFIED blockers rescues
the two hard-blocked classification threads, without breaking clean ones.

SAFETY CONTRACT (do not break):
  - Reads trigger vocabulary from results/VERIFIED_BLOCKERS.json +
    vocab/isolated_triggers_*.json + vocab/swap_map.json — terms never printed.
  - Full detail (incl. residual triggers, minimal pieces) goes to
    results/acceptance_rescue.json (file only). Stdout = ids/counts/verdicts.

Pipeline:
  1. merged map = existing swap_map + verified_single ([pol-term])
     + isolated phrases ([pol-phrase]); dedupe (existing keys win).
  2. per hard thread: c0 verbatim (expect BLOCK) → c1 merged-swap applied to
     the FULL prompt string → probe.
  3. residual: if c1 still blocks, window+bisect the c1 prompt (pattern from
     scripts/bisect_blocked.py), verify candidate words/pairs against an
     innocent carrier, add verified residuals to the map, re-swap, re-probe.
  4. spot-check: 5 recent storied pol threads, swapped prompts must PASS.
  5. on full success: archive + deploy merged map to
     chanalyse ops/filter_vocab/swap_map.json (same schema).

Run (from chanalyse root):
  ~/projects/MASTER_PROJECTS/chanalyse/engine/.venv/bin/python3 \
    /home/fivelidz/projects/GLM_projects/investigation/glm_word_filter/scripts/acceptance_rescue.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import httpx

WORK = Path("/home/fivelidz/projects/GLM_projects/investigation/glm_word_filter")
RESULTS = WORK / "results"
LOGS = RESULTS / "logs"
ENGINE = Path.home() / "projects/MASTER_PROJECTS/chanalyse/engine"
CHANALYSE = ENGINE.parent
OPS_VOCAB = CHANALYSE / "ops/filter_vocab"
sys.path.insert(0, str(ENGINE))

DB = Path.home() / ".cache/chanalyse/gmktec.db"
KNOWN_HARD = [136080, 138356]
CALL_CAP = 60
SLEEP = 1.2
MIN_PIECE = 60
WIN_MIN, WIN_MAX = 200, 400
WRAPPER = "Please review the following excerpt and reply with the single word DONE.\n\n"
CARRIER = (
    "The weather report for the coastal region mentioned strong winds and "
    "light rain expected by evening, with temperatures staying mild throughout."
)
MAX_BISECT_WINDOWS = 2  # blocking windows bisected per thread (budget guard)
MAX_CAND_SINGLES = 8
MAX_CAND_PAIRS = 6

T_POL = "[pol-term]"
T_PHRASE = "[pol-phrase]"


# ── env: this investigation's .env ZAI_* feed the GLM_* overrides ───────────
def prime_env() -> None:
    local = {}
    p = WORK / ".env"
    if p.exists():
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                local[k.strip()] = v.strip()
    for src, dst in (
        ("ZAI_API_KEY", "GLM_API_KEY"),
        ("ZAI_BASE_URL", "GLM_BASE_URL"),
        ("ZAI_MODEL", "GLM_MODEL"),
    ):
        if local.get(src):
            os.environ[dst] = local[src]


def load_env() -> dict:
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


# ── sanitized probe ──────────────────────────────────────────────────────────
class Probe:
    """(blocked, detail); detail never contains content."""

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
            except Exception as e:
                if attempt == 1:
                    self.net_errs += 1
                    return False, f"net-err-{type(e).__name__}"
                time.sleep(2.0)
        if r is None:
            return False, "net-err-no-response"
        if r.status_code == 200:
            return False, "ok"
        body = r.text[:300]  # in-memory only, never printed/stored
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


# ── swap machinery (filter_probe.swap pattern, map-driven) ──────────────────
def compile_map(map_: dict[str, str]) -> list[tuple[re.Pattern, str]]:
    """Longest-first so phrases beat their component words."""
    out = []
    for term in sorted(map_, key=lambda t: -len(t)):
        token = map_[term]
        rx = re.compile(
            r"(?<![\w])" + re.escape(term).replace(r"\ ", r"[\s\-_.]+") + r"(?![\w])",
            re.IGNORECASE,
        )
        out.append((rx, token))
    return out


def swap_full(text: str, compiled) -> tuple[str, int]:
    n = 0
    for rx, token in compiled:
        text, k = rx.subn(token, text)
        n += k
    return text, n


# ── windowing + bisection (bisect_blocked.py pattern) ───────────────────────
def split_units(text: str) -> list[str]:
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
    if "".join(windows) != prompt:
        windows = pack(split_units(prompt), WIN_MAX)
    if "".join(windows) != prompt:
        windows = pack([" ".join(prompt.split(" "))], WIN_MAX)
    return windows


def split_at_word(piece: str) -> tuple[str, str]:
    mid = len(piece) // 2
    li = piece.rfind(" ", 0, mid)
    ri = piece.find(" ", mid)
    if li == -1 and ri == -1:
        return piece[:mid], piece[mid:]
    cut = li if ri == -1 or (li != -1 and (mid - li) <= (ri - mid)) else ri
    return piece[:cut], piece[cut + 1 :]


def bisect(
    pr: Probe, piece: str, pieces: list[str], state: dict, depth: int = 0
) -> None:
    if pr.cap_hit or pr.calls >= CALL_CAP - 4:
        state["budget_stop"] += 1
        return
    if len(piece) < MIN_PIECE or depth > 8:
        pieces.append(piece)
        return
    a, b = split_at_word(piece)
    verdicts = []
    for half in (a, b):
        if pr.calls >= CALL_CAP - 4:
            pr.cap_hit = True
            break
        if not half.strip():
            verdicts.append("skip")
            continue
        blk, _det = pr.probe(half)
        verdicts.append(blk)
        if blk:
            bisect(pr, half, pieces, state, depth + 1)
    if verdicts and all(v is False for v in verdicts):
        state["resistant"] += 1


# ── residual-trigger isolation (script-side only; never printed) ────────────
def extract_candidate_terms(pieces: list[str], known: set[str]) -> list[str]:
    """Single words first, then adjacent pairs, deduped, known terms skipped."""
    singles: list[str] = []
    pairs: list[str] = []
    seen: set[str] = set()
    for piece in pieces:
        words = [w for w in re.findall(r"[A-Za-z][A-Za-z'’\-]+", piece) if len(w) >= 2]
        for i, w in enumerate(words):
            k = w.casefold()
            if k not in seen and k not in known:
                seen.add(k)
                singles.append(w)
            if i + 1 < len(words):
                pw = f"{w} {words[i + 1]}"
                pk = pw.casefold()
                if pk not in seen and pk not in known:
                    seen.add(pk)
                    pairs.append(pw)
    return singles[:MAX_CAND_SINGLES] + pairs[:MAX_CAND_PAIRS]


def verify_blockers(pr: Probe, cands: list[str], known: set[str]) -> dict[str, str]:
    """Probe each candidate against the innocent carrier (raw call).
    Returns {candidate: token} for candidates that block alone."""
    found: dict[str, str] = {}
    for cand in cands:
        if pr.calls >= CALL_CAP - 3:
            break
        if cand.casefold() in known:
            continue
        blk, _det = pr.probe(CARRIER + " " + cand, raw=True)
        if blk:
            found[cand] = T_PHRASE if " " in cand else T_POL
    return found


# ── chanalyse prompt build (verbatim filter_probe/bisect pattern) ───────────
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


def recent_storied_pol_ids(n: int) -> list[int]:
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = db.execute(
        "SELECT id FROM threads WHERE board='pol' AND is_storied=1 "
        "ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()
    db.close()
    return [r[0] for r in rows]


# ── map building ─────────────────────────────────────────────────────────────
def build_merged() -> tuple[dict[str, str], dict]:
    existing_doc = json.load(open(WORK / "vocab/swap_map.json"))
    existing: dict[str, str] = dict(existing_doc["map"])
    vb = json.load(open(RESULTS / "VERIFIED_BLOCKERS.json"))
    verified: list[str] = list(vb["verified_single"])
    iso_doc = json.load(open(WORK / "vocab/isolated_triggers_20260830_084738.json"))
    phrases: list[str] = [
        p for r in iso_doc["results"] for p in r.get("isolated_minimal", [])
    ]

    known_keys = {k.casefold() for k in existing}
    merged = dict(existing)
    added_v = added_p = dup_v = dup_p = 0
    for t in verified:
        if t.casefold() in known_keys:
            dup_v += 1
            continue
        merged[t] = T_POL
        known_keys.add(t.casefold())
        added_v += 1
    for p in phrases:
        if p.casefold() in known_keys:
            dup_p += 1
            continue
        merged[p] = T_PHRASE
        known_keys.add(p.casefold())
        added_p += 1
    stats = {
        "existing": len(existing),
        "verified_single": len(verified),
        "isolated_phrases": len(phrases),
        "added_from_verified": added_v,
        "added_from_isolated": added_p,
        "dupes_skipped": dup_v + dup_p,
        "merged_total": len(merged),
    }
    return merged, stats


def deploy(merged: dict[str, str]) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    arch = OPS_VOCAB / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    target = OPS_VOCAB / "swap_map.json"
    if target.is_file():
        shutil.copy2(target, arch / f"swap_map_{ts}.json")
    doc = {
        "_meta": {
            "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": "verified glm_word_filter investigation",
        },
        "map": merged,
    }
    target.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    return f"{target} ({len(merged)} terms; archived prior as swap_map_{ts}.json)"


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    prime_env()
    env = load_env()
    pr = Probe(env)
    merged, mstats = build_merged()
    compiled = compile_map(merged)
    print(
        f"[map] existing={mstats['existing']} +verified={mstats['added_from_verified']}"
        f"(of {mstats['verified_single']}) +phrases={mstats['added_from_isolated']}"
        f"(of {mstats['isolated_phrases']}) dupes={mstats['dupes_skipped']}"
        f" -> merged={mstats['merged_total']}"
    )

    results: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": env.get("GLM_MODEL"),
        "map_stats": mstats,
        "threads": [],
        "spot_checks": [],
        "residual_rounds": [],
        "deployed": None,
    }

    # baseline sanity
    b, d = pr.probe(CARRIER, raw=True)
    results["carrier_baseline"] = {"blocked": b, "detail": d}
    print(f"[probe] carrier baseline: {'BLOCKED' if b else 'pass'} ({d})")

    known_keys = {k.casefold() for k in merged}
    success = True

    for tid in KNOWN_HARD:
        prompt = build_prompt_for(tid)
        row: dict = {"id": tid}

        c0_blk, c0_det = pr.probe(prompt, raw=True)
        row["c0"] = {"blocked": c0_blk, "detail": c0_det}
        if c0_blk is False:
            row["verdict"] = "filter-drift (c0 passes now)"
            results["threads"].append(row)
            print(f"id={tid} c0=pass (filter-drift) — skipped")
            continue
        if c0_blk is None:
            row["verdict"] = "untestable"
            results["threads"].append(row)
            print(f"id={tid} c0=UNTESTABLE ({c0_det})")
            success = False
            continue
        print(f"id={tid} c0=BLK ({c0_det})")

        c1_text, nsw = swap_full(prompt, compiled)
        c1_blk, c1_det = pr.probe(c1_text, raw=True)
        row["c1"] = {"blocked": c1_blk, "detail": c1_det, "swapped": nsw}
        print(f"id={tid} c0=BLK c1={'BLK' if c1_blk else 'pass'} swapped={nsw}")

        if c1_blk is False:
            row["verdict"] = "rescued-round-1"
            results["threads"].append(row)
            continue
        if c1_blk is None:
            row["verdict"] = "untestable-c1"
            results["threads"].append(row)
            success = False
            continue

        # ── adaptive round 2: isolate residual trigger ──────────────────
        r2: dict = {
            "id": tid,
            "windows_total": 0,
            "blocking_windows": 0,
            "minimal_pieces": 0,
            "resistant": 0,
            "candidates": 0,
            "residual_added": 0,
            "recount_swapped": None,
            "recheck": None,
        }
        windows = make_windows(c1_text)
        r2["windows_total"] = len(windows)
        blocking_wins: list[str] = []
        for w in windows:
            if pr.calls >= CALL_CAP - 6:
                pr.cap_hit = True
                break
            blk, _det = pr.probe(w)
            if blk:
                blocking_wins.append(w)
        r2["blocking_windows"] = len(blocking_wins)

        pieces: list[str] = []
        state = {"resistant": 0, "budget_stop": 0}
        for w in blocking_wins[:MAX_BISECT_WINDOWS]:
            bisect(pr, w, pieces, state)
        r2["minimal_pieces"] = len(pieces)
        r2["resistant"] = state["resistant"]
        r2["piece_samples"] = pieces[:5]  # file only

        cands = extract_candidate_terms(pieces, known_keys)
        r2["candidates"] = len(cands)
        found = verify_blockers(pr, cands, known_keys)
        r2["residual_terms"] = list(found.keys())  # file only
        r2["residual_added"] = len(found)

        if found:
            merged.update(found)
            compiled = compile_map(merged)
            known_keys |= {k.casefold() for k in found}
            prompt_r2 = build_prompt_for(tid)
            text_r2, nsw2 = swap_full(prompt_r2, compiled)
            blk2, det2 = pr.probe(text_r2, raw=True)
            r2["recount_swapped"] = nsw2
            r2["recheck"] = {"blocked": blk2, "detail": det2}
            print(
                f"id={tid} round=2 c0=BLK c1={'BLK' if blk2 else 'pass'} "
                f"swapped={nsw2} residual_added={len(found)}"
            )
            row["c1_round2"] = r2["recheck"]
            row["round2"] = r2
            row["verdict"] = "rescued-round-2" if blk2 is False else "still-blocked"
            if blk2 is not False:
                success = False
        else:
            r2["recheck"] = None
            row["round2"] = r2
            row["verdict"] = "residual-not-isolated"
            print(f"id={tid} round=2 residual-not-isolated (still blocked)")
            success = False
        results["residual_rounds"].append(r2)
        results["threads"].append(row)

    # ── spot-check: 5 recent storied pol threads, swapped must pass ─────
    spot_ids = recent_storied_pol_ids(5)
    spot_pass = 0
    for sid in spot_ids:
        try:
            sp = build_prompt_for(sid)
        except LookupError:
            results["spot_checks"].append({"id": sid, "verdict": "missing"})
            print(f"SPOT id={sid} missing")
            continue
        stext, sn = swap_full(sp, compiled)
        if pr.calls >= CALL_CAP:
            pr.cap_hit = True
            results["spot_checks"].append({"id": sid, "verdict": "cap"})
            print(f"SPOT id={sid} SKIPPED (cap)")
            success = False
            continue
        blk, det = pr.probe(stext, raw=True)
        ok = blk is False
        spot_pass += int(ok)
        results["spot_checks"].append(
            {
                "id": sid,
                "blocked": blk,
                "detail": det,
                "swapped": sn,
                "verdict": "pass" if ok else "BLOCKED",
            }
        )
        print(f"SPOT id={sid} {'pass' if ok else 'BLK'} swapped={sn}")
        if not ok:
            success = False

    # ── verdicts + artifacts ────────────────────────────────────────────
    rescued = sum(
        1 for t in results["threads"] if str(t.get("verdict", "")).startswith("rescued")
    )
    drift = sum(1 for t in results["threads"] if "drift" in str(t.get("verdict", "")))
    print(
        f"[SUMMARY] rescued={rescued}/{len(KNOWN_HARD)} drift={drift} "
        f"spot_pass={spot_pass}/{len(spot_ids)} api_calls={pr.calls} "
        f"map_merged={len(merged)}"
    )

    if (
        success
        and all(
            str(t.get("verdict", "")).startswith(("rescued", "filter-drift"))
            for t in results["threads"]
        )
        and spot_pass == len(spot_ids)
    ):
        deployed = deploy(merged)
        results["deployed"] = deployed
        print(f"[DEPLOY] deployed={len(merged)} -> {deployed}")
    else:
        print("[DEPLOY] SKIPPED (success criteria not met)")

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / "acceptance_rescue.json"
    out.write_text(json.dumps(results, indent=1, ensure_ascii=False))
    print(f"[WRITE] {out} (file only)")

    md = RESULTS / "ACCEPTANCE_SUMMARY.md"
    lines = [
        "# Acceptance: verified-blocker swap map rescue",
        "",
        f"- Run: {results['ts']}  |  model: `{results['model']}`",
        f"- API calls: {pr.calls} (cap {CALL_CAP})",
        "",
        "## Map",
        f"- existing: {mstats['existing']}",
        f"- + verified_single -> [pol-term]: {mstats['added_from_verified']}/{mstats['verified_single']}",
        f"- + isolated phrases -> [pol-phrase]: {mstats['added_from_isolated']}/{mstats['isolated_phrases']}",
        f"- dupes skipped: {mstats['dupes_skipped']}",
        f"- merged total: {mstats['merged_total']} (pre-round-2); final: {len(merged)}",
        "",
        "## Hard threads",
    ]
    for t in results["threads"]:
        lines.append(
            f"- id={t['id']}: c0={'BLK' if t.get('c0', {}).get('blocked') else 'pass'}"
            f" c1={'BLK' if t.get('c1', {}).get('blocked') else 'pass'}"
            f" swapped={t.get('c1', {}).get('swapped')}"
            f" verdict={t.get('verdict')}"
            + (
                f" round2={'pass' if not t['c1_round2']['blocked'] else 'BLK'}"
                if t.get("c1_round2")
                else ""
            )
        )
    lines += ["", "## Spot checks (recent storied pol, swapped)"]
    for s in results["spot_checks"]:
        lines.append(f"- id={s['id']}: {s['verdict']} (swapped={s.get('swapped')})")
    lines += [
        "",
        f"## Result: {'SUCCESS' if success else 'FAILURE'}"
        f" — rescued {rescued}/{len(KNOWN_HARD)}, spot {spot_pass}/{len(spot_ids)}",
        "",
        f"Deployed: {results['deployed'] or 'no'}",
        "",
        "Residual triggers (count only): "
        + str(sum(r.get("residual_added", 0) for r in results["residual_rounds"])),
    ]
    md.write_text("\n".join(lines) + "\n")
    print(f"[WRITE] {md}")


if __name__ == "__main__":
    main()
