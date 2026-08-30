#!/usr/bin/env python3
"""probe_collocations.py — do individually-CLEAN terms block as combinations?

Motivation: the isolated trigger was a 3-word phrase whose individual words and
bigrams all PASSED — only the trigram blocked. Many lexicon entries look
innocuous alone. This probes whether clean singles block when combined.

Design:
  - Load all verdicts (singles) → clean set, per fine-category (from v2 lexicon).
  - Category-pair matrix (semantically related groups):
      persons   × events        (dissidents/leaders/en_persons × events_*/disasters)
      orgs      × events        (movements_orgs/en_banned_media_orgs × events_*)
      regions   × policies      (regions_territorial/regions_places × policies_*)
      memes     × memes         (zh_homophone_memes × itself)
      persons   × accusations   (persons × ccp_critical_phrases/en_phrases_deep)
      dates     × events        (blocked_dates_anniversaries × events_*)
  - Sample N pairs per group (seeded), probe: carrier + " " + A + " " + B.
  - Sample M triples from the hottest group (persons × events × accusation verbs
    from en_phrases_deep) — the proven trigram pattern.
  - CONTROL: also re-probe ~20 of the A terms alone again (stability check).

Outputs: results/collocation_verdicts.jsonl, results/logs/collocation_counts.json
SAFETY: file-to-file; stdout counts only.
"""

from __future__ import annotations

import glob
import json
import os
import random
import sys
import time

import httpx

WORK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(WORK)
random.seed(20260830)

CARRIER = (
    "The weather report for the coastal region mentioned strong winds and "
    "light rain expected by evening, with temperatures staying mild throughout."
)
PAIRS_PER_GROUP = 18
TRIPLES = 30
CONTROLS = 20
SLEEP = 1.0
CALL_CAP = 400

PERSON_CATS = {
    "dissidents_activists",
    "leaders_family",
    "leaders_names",
    "en_persons",
    "journalists_media_banned",
}
EVENT_CATS = {
    "events_1949_1959",
    "events_1960_1979",
    "events_1980_1999",
    "events_2000_2012",
    "events_2013_2026",
    "historical_events",
    "disasters_suppressed",
}
ORG_CATS = {"movements_organizations", "movements_orgs", "en_banned_media_orgs"}
REGION_CATS = {"regions_territorial", "regions_places"}
POLICY_CATS = {"policies_campaigns", "policies_ideology"}
MEME_CATS = {"zh_homophone_memes", "zh_pinyin_abbreviations", "zh_splitchar_forms"}
ACCUSE_CATS = {
    "ccp_critical_phrases",
    "en_phrases_deep",
    "en_slogans",
    "en_collocations_name_event",
}
DATE_CATS = {"blocked_dates_anniversaries", "incident_number_codes"}

GROUPS = [
    ("person_x_event", PERSON_CATS, EVENT_CATS),
    ("org_x_event", ORG_CATS, EVENT_CATS),
    ("region_x_policy", REGION_CATS, POLICY_CATS),
    ("meme_x_meme", MEME_CATS, MEME_CATS),
    ("person_x_accusation", PERSON_CATS, ACCUSE_CATS),
    ("date_x_event", DATE_CATS, EVENT_CATS),
]


def load_env() -> dict:
    env = {}
    for line in open(".env"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


class Probe:
    def __init__(self, env):
        self.headers = {
            "x-api-key": env["ZAI_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        self.base, self.model = env["ZAI_BASE_URL"], env["ZAI_MODEL"]
        self.calls = 0
        self.consec_err = 0

    def call(self, prompt):
        r = None
        for attempt in (0, 1):
            self.calls += 1
            try:
                r = httpx.post(
                    self.base,
                    json={
                        "model": self.model,
                        "max_tokens": 64,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    headers=self.headers,
                    timeout=120,
                )
                break
            except Exception:
                if attempt == 1:
                    return False, "net-err"
                time.sleep(2.0)
        if r is None:
            return False, "net-err"
        if r.status_code == 200:
            self.consec_err = 0
            return False, "ok"
        body = r.text[:300]
        blocked = ("1301" in body) or ("sensitive" in body.lower())
        if blocked:
            self.consec_err = 0
            return True, f"HTTP{r.status_code}-1301"
        self.consec_err += 1
        return False, f"HTTP{r.status_code}"


def load_clean_singles() -> dict[str, list[str]]:
    """term -> verdict from all single-term verdict files; return cat->clean terms."""
    tested: dict[str, bool] = {}
    for f in (
        glob.glob("results/term_verdicts_*.jsonl")
        + glob.glob("results/pool_verdicts_*.jsonl")
        + glob.glob("results/lexicon_verdicts_v*.jsonl")
    ):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            t = rec.get("term")
            if t:
                tested[t] = bool(rec.get("blocked"))
    # v2 lexicon for fine categories
    by_cat: dict[str, list[str]] = {}
    try:
        lx = json.load(open("vocab/antiscrape_lexicon_v2.json", encoding="utf-8"))
        for cat, lst in lx.items():
            if cat.startswith("_") or not isinstance(lst, list):
                continue
            by_cat.setdefault(cat, [])
            for t in lst:
                if isinstance(t, str) and tested.get(t) is False:  # CLEAN singles only
                    by_cat[cat].append(t)
    except FileNotFoundError:
        pass
    # v1 nested cats too
    try:
        lx1 = json.load(open("vocab/antiscrape_lexicon.json", encoding="utf-8"))
        for grp in ("zh", "en", "phrases"):
            sub = lx1.get(grp)
            if isinstance(sub, dict):
                for cat, lst in sub.items():
                    if isinstance(lst, list):
                        by_cat.setdefault(cat, [])
                        for t in lst:
                            if isinstance(t, str) and tested.get(t) is False:
                                by_cat[cat].append(t)
    except FileNotFoundError:
        pass
    return {c: sorted(set(v)) for c, v in by_cat.items() if v}


def main() -> int:
    env = load_env()
    probe = Probe(env)

    # sanity
    b, d = probe.call(CARRIER)
    if b:
        print("[abort] carrier blocked")
        return 2
    time.sleep(SLEEP)

    by_cat = load_clean_singles()
    print(
        f"[plan] clean singles by cat: {sum(len(v) for v in by_cat.values())} "
        f"across {len(by_cat)} cats"
    )

    out = open("results/collocation_verdicts.jsonl", "a", encoding="utf-8")

    def rec(**kw):
        out.write(json.dumps(kw, ensure_ascii=False) + "\n")
        out.flush()

    stats = {}

    # pairs
    for gname, cats_a, cats_b in GROUPS:
        pool_a = sorted({t for c in cats_a if c in by_cat for t in by_cat[c]})
        pool_b = sorted({t for c in cats_b if c in by_cat for t in by_cat[c]})
        if not pool_a or not pool_b:
            stats[gname] = {"pairs": 0, "blocked": 0, "skip": "empty pool"}
            continue
        combos = [
            (a, b)
            for a in random.sample(pool_a, min(len(pool_a), 40))
            for b in random.sample(pool_b, min(len(pool_b), 40))
        ]
        random.shuffle(combos)
        blocked_n = done = 0
        for a, b in combos:
            if done >= PAIRS_PER_GROUP or probe.calls >= CALL_CAP:
                break
            bl, det = probe.call(CARRIER + " " + a + " " + b)
            rec(kind="pair", group=gname, a=a, b=b, blocked=bl, detail=det)
            time.sleep(SLEEP)
            done += 1
            blocked_n += int(bl)
            if probe.consec_err >= 5:
                stats[gname] = {"pairs": done, "blocked": blocked_n, "abort": "drift"}
                break
        stats[gname] = {"pairs": done, "blocked": blocked_n}
        print(f"[{gname}] pairs={done} blocked={blocked_n}")

    # triples (persons x events x accusation) — the proven pattern
    pool_p = sorted({t for c in PERSON_CATS if c in by_cat for t in by_cat[c]})
    pool_e = sorted({t for c in EVENT_CATS if c in by_cat for t in by_cat[c]})
    pool_x = sorted({t for c in ACCUSE_CATS if c in by_cat for t in by_cat[c]})
    tri_done = tri_blk = 0
    if pool_p and pool_e and pool_x:
        for _ in range(TRIPLES):
            if probe.calls >= CALL_CAP:
                break
            tri = " ".join(
                (random.choice(pool_p), random.choice(pool_e), random.choice(pool_x))
            )
            bl, det = probe.call(CARRIER + " " + tri)
            rec(
                kind="triple",
                group="person_event_accusation",
                text=tri,
                blocked=bl,
                detail=det,
            )
            time.sleep(SLEEP)
            tri_done += 1
            tri_blk += int(bl)
            if probe.consec_err >= 5:
                break
    stats["triples"] = {"tested": tri_done, "blocked": tri_blk}
    print(f"[triples] tested={tri_done} blocked={tri_blk}")

    # stability controls: re-probe some single clean terms
    ctrl_pool = [t for c in PERSON_CATS | EVENT_CATS if c in by_cat for t in by_cat[c]][
        :50
    ]
    random.shuffle(ctrl_pool)
    ctrl_done = ctrl_blk = 0
    for t in ctrl_pool:
        if ctrl_done >= CONTROLS or probe.calls >= CALL_CAP:
            break
        bl, det = probe.call(CARRIER + " " + t)
        rec(kind="control_single", term=t, blocked=bl, detail=det)
        time.sleep(SLEEP)
        ctrl_done += 1
        ctrl_blk += int(bl)
    stats["controls"] = {"tested": ctrl_done, "blocked": ctrl_blk}
    print(f"[controls] tested={ctrl_done} blocked={ctrl_blk}")

    counts = {
        "stats": stats,
        "calls": probe.calls,
        "wall_s": time.time(),
        "cap": CALL_CAP,
    }
    json.dump(counts, open("results/logs/collocation_counts.json", "w"), indent=1)
    print(json.dumps(stats, indent=1))
    out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
