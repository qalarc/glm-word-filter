#!/usr/bin/env python3
"""verify_pool.py — verify vocab/candidate_pool.json terms against the GLM 1301
content filter, plus an n-gram/template context layer.

SAFETY CONTRACT (do not break):
  - Term content NEVER reaches stdout. stdout = counts/categories/paths only.
  - Terms written ONLY to results/pool_verdicts_<ts>.jsonl (flush+fsync/line).
  - Env: WORK/.env (ZAI_API_KEY / ZAI_BASE_URL / ZAI_MODEL); never printed.
  - blocked = status!=200 and ("1301" in body or "sensitive" in body.lower()).
  - 1.0s sleep between calls; single retry on network errors; hard cap 600
    calls total, 120 calls in the context layer.
  - Sanity gate: bare carrier must pass (HTTP 200) or the run ABORTS.

Layers
  MAIN    carrier + " " + term for every pool term not already tested.
          Prior verdicts (results/term_verdicts_20260830_1121*.jsonl) are
          carried forward into the output file without re-calling.
  CONTEXT 3 templates x (blocked terms ∪ trigger_expanded core_terms):
            T1 "Analysts noted that TERM appeared in the document."
            T2 "The archive contains one file mentioning TERM from last year."
            T3 carrier + " " + TERM
          T3 is REUSED (no HTTP call) when a carrier verdict already exists
          for the term (same-run main layer or prior verdict file).
          Rows use category field = "T1"/"T2"/"T3".

Call pattern mirrors scripts/probe_terms.py / scripts/bisect_blocked.py
(x-api-key + anthropic-version 2023-06-01; POST {model, max_tokens, messages};
timeout 120; max_tokens 8192).

Run:  python3 scripts/verify_pool.py 2>&1 | tee results/logs/verify_pool.log
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
WORK = HERE.parent
ENV_FILE = WORK / ".env"

POOL_FILE = WORK / "vocab/candidate_pool.json"
SEEDS_FILE = WORK / "vocab/local_seeds.json"
SWAP_FILE = WORK / "vocab/swap_map.json"
POL_FILE = WORK / "vocab/political_terms.txt"
TRIG_FILE = WORK / "vocab/trigger_expanded.json"
PRIOR_FILES = [
    WORK / "results/term_verdicts_20260830_112028.jsonl",  # superseded
    WORK / "results/term_verdicts_20260830_112149.jsonl",  # newer wins
]

CARRIER = (
    "The weather report for the coastal region mentioned strong winds and "
    "light rain expected by evening, with temperatures staying mild throughout."
)
# T1/T2 are fresh probes; T3 is the carrier sentence (reused when known).
TEMPLATES: list[tuple[str, str]] = [
    ("T1", "Analysts noted that {} appeared in the document."),
    ("T2", "The archive contains one file mentioning {} from last year."),
    ("T3", CARRIER + " {}"),
]
TMPL_IDS = {"T1", "T2", "T3"}

CALL_CAP = 600  # hard cap on HTTP attempts overall (retries included)
CTX_CAP = 120  # hard cap on HTTP attempts inside the context layer
SLEEP = 1.0
MAX_TOKENS = 8192
TIMEOUT = 120


def load_env() -> dict:
    """WORK/.env only; os.environ ZAI_* overrides (mirrors probe_terms)."""
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


class Probe:
    """Sanitized API probe: (blocked, detail) — detail never contains content."""

    def __init__(self, env: dict):
        self.headers = {
            "x-api-key": env["ZAI_API_KEY"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        self.base, self.model = env["ZAI_BASE_URL"], env["ZAI_MODEL"]
        self.calls = 0
        self.net_errs = 0

    def call(self, prompt: str) -> tuple[bool, str]:
        """One HTTP attempt; single retry on network-layer errors only."""
        r: httpx.Response | None = None
        for attempt in (0, 1):
            self.calls += 1
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
                    return False, f"net-err-{type(e).__name__}"
                time.sleep(2.0)
        if r is None:
            return False, "net-err-no-response"
        if r.status_code == 200:
            return False, "ok"
        body = r.text[:300]  # inspected in-memory only, never printed
        blocked = ("1301" in body) or ("sensitive" in body.lower())
        return blocked, f"HTTP{r.status_code}{'-1301' if blocked else ''}"

    def probe(self, prompt: str) -> tuple[bool | None, str]:
        """None verdict => cap hit or persistent net error (could not test)."""
        if self.calls >= CALL_CAP:
            return None, "cap"
        blocked, detail = self.call(prompt)
        time.sleep(SLEEP)
        if detail.startswith("net-err"):
            return None, detail
        return blocked, detail


# ── input discovery (structure-tolerant; prints counts/names only) ──────────


def load_pool() -> list[str]:
    data = json.loads(POOL_FILE.read_text())
    cands = data.get("candidates", data if isinstance(data, list) else [])
    out, seen = [], set()
    for t in cands:
        if isinstance(t, str) and t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def load_seed_categories() -> dict[str, set[str]]:
    """local_seeds.json -> {category: set(terms)}; tolerates several shapes."""
    obj = json.loads(SEEDS_FILE.read_text())
    cats: dict[str, set[str]] = {}

    def add(cat: str, val) -> None:
        if isinstance(val, str):
            cats.setdefault(cat, set()).add(val)
        elif isinstance(val, list):
            for v in val:
                if isinstance(v, str):
                    cats.setdefault(cat, set()).add(v)

    def entry_add(entry) -> None:
        if not isinstance(entry, dict):
            return
        cat = entry.get("category") or entry.get("name") or entry.get("cat")
        if not cat:
            return
        for key in ("terms", "items", "words", "list", "entries"):
            if key in entry:
                add(str(cat), entry[key])
                return

    if isinstance(obj, dict):
        inner = obj.get("categories", obj)
        if isinstance(inner, dict):
            for cat, val in inner.items():
                if isinstance(val, dict):
                    got = False
                    for key in ("terms", "items", "words", "list", "entries"):
                        if key in val:
                            add(str(cat), val[key])
                            got = True
                            break
                    if not got:  # dict of term->something; keep the keys
                        add(str(cat), [k for k in val if isinstance(k, str)])
                else:
                    add(str(cat), val)
        elif isinstance(inner, list):
            for entry in inner:
                entry_add(entry)
    elif isinstance(obj, list):
        for entry in obj:
            entry_add(entry)
    return cats


def load_swap_categories() -> dict[str, str]:
    """swap_map.json: term -> placeholder ("[eth-slur]" / "[pol-term]")."""
    obj = json.loads(SWAP_FILE.read_text())
    pairs: list[tuple] = []
    if isinstance(obj, dict):
        pairs = list(obj.items())
    elif isinstance(obj, list):
        for e in obj:
            if isinstance(e, dict):
                pairs.append(
                    (
                        e.get("term") or e.get("original"),
                        e.get("placeholder") or e.get("category") or e.get("swap"),
                    )
                )
    mapping: dict[str, str] = {}
    for term, ph in pairs:
        if not isinstance(term, str) or not term:
            continue
        s = ph if isinstance(ph, str) else json.dumps(ph, ensure_ascii=False)
        s = s.lower()
        if "pol" in s:
            mapping[term] = "political_cn"
        elif "eth" in s or "slur" in s:
            mapping[term] = "ethnic_slur"
    return mapping


def load_pol_terms() -> set[str]:
    out: set[str] = set()
    for line in POL_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:  # term<TAB>extra -> keep term column
            line = line.split("\t", 1)[0].strip()
        if line:
            out.add(line)
    return out


TRIG_KEYS = {"core_terms", "core", "variants", "variant", "related", "related_terms"}


def load_trigger_terms() -> tuple[set[str], set[str]]:
    """(core_terms, variants+related) via recursive scan of trigger_expanded."""
    core: set[str] = set()
    rel: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if isinstance(v, list) and kl in TRIG_KEYS:
                    tgt = core if kl in ("core_terms", "core") else rel
                    for x in v:
                        if isinstance(x, str):
                            tgt.add(x)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads(TRIG_FILE.read_text()))
    return core, rel


def load_prior() -> dict[str, tuple[bool, str]]:
    """term -> (blocked, detail) from prior verdict files; newest file wins.
    net-err / cap rows are NOT real verdicts -> omitted (term gets retested)."""
    priors: dict[str, tuple[bool, str]] = {}
    for pf in PRIOR_FILES:
        if not pf.exists():
            continue
        for line in pf.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            term = r.get("term")
            if not isinstance(term, str) or not term or "blocked" not in r:
                continue
            detail = str(r.get("detail", ""))
            if detail.startswith("net-err") or detail == "cap":
                continue
            priors[term] = (bool(r["blocked"]), detail)
    return priors


class CategoryResolver:
    """Lookup order: local_seeds -> swap_map -> political_terms.txt ->
    trigger_expanded (core/variants/related -> political_cn) -> "unmapped".
    Exact match first, then case-insensitive fallback."""

    ORDER = ("seeds", "swap", "pol", "trig")

    def __init__(
        self,
        seeds: dict[str, set[str]],
        swap: dict[str, str],
        pol: set[str],
        trig: set[str],
    ):
        self.exact: dict[str, dict[str, str]] = {s: {} for s in self.ORDER}
        self.lower: dict[str, dict[str, str]] = {s: {} for s in self.ORDER}
        for cat, terms in seeds.items():
            for t in terms:
                self._put("seeds", t, cat)
        for t, cat in swap.items():
            self._put("swap", t, cat)
        for t in pol:
            self._put("pol", t, "political")
        for t in trig:
            self._put("trig", t, "political_cn")

    def _put(self, src: str, term: str, cat: str) -> None:
        self.exact[src].setdefault(term, cat)
        self.lower[src].setdefault(term.lower(), cat)

    def cat(self, term: str) -> str:
        for src in self.ORDER:
            c = self.exact[src].get(term)
            if c:
                return c
        tl = term.lower()
        for src in self.ORDER:
            c = self.lower[src].get(tl)
            if c:
                return c
        return "unmapped"


def round_robin(*groups: list[str]) -> list[str]:
    """Interleave groups (dedup, order-stable) so no group starves the cap."""
    queues = [list(g) for g in groups]
    targets: list[str] = []
    seen: set[str] = set()
    while any(queues):
        for q in queues:
            while q:
                t = q.pop(0)
                if t not in seen:
                    seen.add(t)
                    targets.append(t)
                    break
    return targets


def main() -> int:
    t0 = time.time()
    env = load_env()
    missing = [
        k for k in ("ZAI_API_KEY", "ZAI_BASE_URL", "ZAI_MODEL") if not env.get(k)
    ]
    if missing:
        print(f"FATAL: missing env keys: {missing}")
        return 2

    # ── load inputs ──────────────────────────────────────────────────────
    pool = load_pool()
    prior = load_prior()
    seeds = load_seed_categories()
    swap = load_swap_categories()
    pol = load_pol_terms()
    trig_core, trig_rel = load_trigger_terms()
    resolver = CategoryResolver(seeds, swap, pol, trig_core | trig_rel)

    print(
        f"DISCOVERY pool={len(pool)} prior={len(prior)} "
        f"seeds_cats={len(seeds)} swap={len(swap)} pol={len(pol)} "
        f"trig_core={len(trig_core)} trig_rel={len(trig_rel)}"
    )
    for cat in sorted(seeds):
        print(f"DISCOVERY_SEED cat={cat} n={len(seeds[cat])}")
    unmapped = sum(1 for t in pool if resolver.cat(t) == "unmapped")
    print(f"DISCOVERY pool_unmapped={unmapped}")

    pr = Probe(env)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = WORK / "results" / f"pool_verdicts_{ts}.jsonl"
    fh = open(out_path, "a", encoding="utf-8")

    def rec(term: str, category: str, blocked: bool, detail: str) -> None:
        fh.write(
            json.dumps(
                {
                    "term": term,
                    "category": category,
                    "blocked": bool(blocked),
                    "detail": detail,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        fh.flush()
        os.fsync(fh.fileno())

    # ── sanity gate: bare carrier must pass, else ABORT ──────────────────
    blocked, detail = pr.call(CARRIER)
    time.sleep(SLEEP)
    if blocked:
        print("SANITY: BLOCKED -> ABORT")
        print(
            f"TOTAL calls={pr.calls} wall={time.time() - t0:.0f}s "
            f"out={out_path.relative_to(WORK)}"
        )
        return 1
    if detail.startswith("net-err"):
        print("SANITY: net-err -> ABORT")
        return 1
    print("SANITY: pass")

    # ── MAIN layer: carry forward prior verdicts, probe the rest ─────────
    cat_stats: dict[str, list[int]] = {}  # cat -> [tested, blocked]
    newly_blocked: list[str] = []
    prior_blocked_all: list[str] = [t for t, (b, _) in prior.items() if b]
    this_run_carrier: dict[str, tuple[bool, str]] = {}
    carried = carried_blocked = retested = main_net = 0
    main_cap_hit = False

    for term in pool:
        cat = resolver.cat(term)
        st = cat_stats.setdefault(cat, [0, 0])
        if term in prior:
            b, d = prior[term]
            rec(term, cat, b, f"prior:{d}")
            carried += 1
            st[0] += 1
            st[1] += int(b)
            carried_blocked += int(b)
            continue
        verdict, d = pr.probe(CARRIER + " " + term)
        if verdict is None:
            if d == "cap":
                main_cap_hit = True
                break
            rec(term, cat, False, d)  # net-err row (not a verdict)
            main_net += 1
            continue
        rec(term, cat, verdict, d)
        this_run_carrier[term] = (verdict, d)
        retested += 1
        st[0] += 1
        st[1] += int(verdict)
        if verdict:
            newly_blocked.append(term)

    print(
        f"MAIN carried={carried} (blocked={carried_blocked}) "
        f"fresh_tested={retested} blocked={len(newly_blocked)} "
        f"net_err={main_net} cap_hit={main_cap_hit}"
    )
    for cat in sorted(cat_stats):
        t, b = cat_stats[cat]
        print(f"CAT {cat} tested={t} blocked={b}")

    # ── CONTEXT layer: templates x (blocked ∪ core_terms) ────────────────
    targets = round_robin(newly_blocked, sorted(trig_core), prior_blocked_all)
    ctx_calls = 0
    ctx_cap_hit = False
    ctx_probed = 0
    tpl_stats: dict[str, list[int]] = {k: [0, 0] for k in TMPL_IDS}

    def carrier_state(t: str) -> bool | None:
        if t in this_run_carrier:
            return this_run_carrier[t][0]
        if t in prior:
            return prior[t][0]
        return None

    for t in targets:
        if pr.calls >= CALL_CAP or ctx_calls >= CTX_CAP:
            ctx_cap_hit = True
            break
        ctx_probed += 1
        cb = carrier_state(t)
        for name, tmpl in TEMPLATES[:2]:  # T1, T2 — always fresh calls
            if pr.calls >= CALL_CAP or ctx_calls >= CTX_CAP:
                ctx_cap_hit = True
                break
            verdict, d = pr.probe(tmpl.format(t))
            ctx_calls += 1
            if verdict is None:
                if d == "cap":
                    ctx_cap_hit = True
                    break
                rec(t, name, False, d)  # net-err row
                continue
            rec(t, name, verdict, d)
            st = tpl_stats[name]
            st[0] += 1
            st[1] += int(verdict)
        if ctx_cap_hit:
            break
        # T3 = carrier + term; reuse a known verdict when one exists.
        if cb is not None:
            if t in this_run_carrier:
                rec(t, "T3", cb, f"reuse-run:{this_run_carrier[t][1]}")
            else:
                rec(t, "T3", cb, f"reuse-prior:{prior[t][1]}")
            st = tpl_stats["T3"]
            st[0] += 1
            st[1] += int(cb)
        else:
            verdict, d = pr.probe(TEMPLATES[2][1].format(t))
            ctx_calls += 1
            if verdict is None:
                if d == "cap":
                    ctx_cap_hit = True
                    break
                rec(t, "T3", False, d)
                continue
            rec(t, "T3", verdict, d)
            st = tpl_stats["T3"]
            st[0] += 1
            st[1] += int(verdict)

    for k in ("T1", "T2", "T3"):
        a, b = tpl_stats[k]
        print(f"CTX {k} tested={a} blocked={b}")
    print(
        f"CTX targets={len(targets)} probed={ctx_probed} http_calls={ctx_calls} "
        f"cap_hit={ctx_cap_hit}"
    )

    print(
        f"CAP total={pr.calls}/{CALL_CAP} net_errs={pr.net_errs} "
        f"wall={time.time() - t0:.0f}s out={out_path.relative_to(WORK)}"
    )
    print("DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
