#!/usr/bin/env python3
"""compare_public.py — overlap of public keyword lists with our verified blocker set.

SAFETY: prints COUNTS ONLY. Never prints any term from any list or the verified set.
Output: results/public_overlap.json (counts/metadata only, no terms).
"""

import base64
import csv
import io
import json
import pickle
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PUB = ROOT / "data" / "pub"
DATA = ROOT / "data"
OUT = ROOT / "results" / "public_overlap.json"


# ---------- restricted unpickler (no arbitrary code execution) ----------
class RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module == "builtins" and name in {
            "list",
            "tuple",
            "set",
            "frozenset",
            "dict",
            "str",
            "int",
            "float",
            "range",
        }:
            return getattr(__import__("builtins"), name)
        raise pickle.UnpicklingError(f"forbidden: {module}.{name}")


def norm(s: str) -> str:
    """NFKC fold (full->half width) + casefold + strip."""
    return unicodedata.normalize("NFKC", str(s)).casefold().strip()


def is_han(t: str) -> bool:
    return any(
        "CJK" in unicodedata.name(ch, "") or "IDEOGRAPH" in unicodedata.name(ch, "")
        for ch in t
    )


def load_txt(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def load_dsplit(path: Path):
    """Delimiter-separated blob (no newlines): split on CN/EN punctuation + whitespace."""
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"[、，,;；|\s]+", text)
    return [p.strip() for p in parts if p and p.strip()]


def load_pipe(path: Path):
    """Pipe-separated blob."""
    text = path.read_text(encoding="utf-8", errors="replace")
    parts = re.split(r"[|\s]+", text)
    return [p.strip() for p in parts if p and p.strip()]


def load_gfwlist(path: Path):
    """gfwlist = base64 body after '#' comment header; returns pattern lines."""
    text = path.read_text(encoding="utf-8", errors="replace")
    b64 = "".join(
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.startswith("[") and not ln.startswith("#")
    )
    try:
        raw = base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception:
        raw = text
    out = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s.startswith("!") or s.startswith("[") or s.startswith("@@"):
            continue
        out.append(s.lstrip("|").rstrip("^*"))
    return [s for s in out if s]


def _cjk_frac(s: str) -> float:
    s = unicodedata.normalize("NFKC", s)
    n = sum(
        1
        for ch in s
        if "CJK" in unicodedata.name(ch, "") or "IDEOGRAPH" in unicodedata.name(ch, "")
    )
    return n / max(1, len(s))


def load_csv_allcells(path: Path):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    cells = [c for row in rows for c in row if c and c.strip()]
    return cells, len(rows)


def load_csv_keyword_col(path: Path):
    """CSV: use keyword column(s) — cells where CJK chars dominate (>30%)."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    n_cols = max(len(r) for r in rows)
    cells = []
    for ci in range(n_cols):
        col = [r[ci] for r in rows if len(r) > ci and r[ci].strip()]
        if not col:
            continue
        dominant = sum(1 for c in col if _cjk_frac(c) > 0.3)
        if dominant / len(col) > 0.5:
            cells.extend(c for c in col if _cjk_frac(c) > 0.3)
    return cells, len(rows)


def load_json_list(path: Path):
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    if isinstance(data, dict):
        data = list(data.values())
    return [str(i).strip() for i in data if str(i).strip()]


def load_pickle_list(path: Path):
    data = RestrictedUnpickler(open(path, "rb")).load()
    items = []
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, (list, tuple, set, frozenset)):
            stack.extend(cur)
        elif isinstance(cur, dict):
            stack.extend(cur.keys())
            stack.extend(cur.values())
        elif isinstance(cur, str):
            items.append(cur.strip())
    return [i for i in items if i]


LOADERS = {
    "tencent_sensitive_words.txt": (
        "dsplit",
        PUB,
        "cjh0613/tencent-sensitive-words (Tencent offline word library; WeChat-context, game-installer extraction)",
        "https://github.com/cjh0613/tencent-sensitive-words",
    ),
    "citizenlab_wechat_keywords.csv": (
        "csvkw",
        PUB,
        "Citizen Lab WeChat keyword list dataset (2020-04-21), via tiktok-report-data repo",
        "https://github.com/citizenlab/tiktok-report-data",
    ),
    "citizenlab_douyin_restricted.csv": (
        "csvkw",
        PUB,
        "Citizen Lab Douyin/TikTok search-restricted hashtags",
        "https://github.com/citizenlab/tiktok-report-data",
    ),
    "houbb_dict_2024.txt": (
        "txt",
        PUB,
        "houbb/sensitive-word bundled test dict v20240407",
        "https://github.com/houbb/sensitive-word",
    ),
    "hahawth_dict.txt": (
        "txt",
        PUB,
        "HaHaWTH/AdvancedSensitiveWords dict",
        "https://github.com/HaHaWTH/AdvancedSensitiveWords",
    ),
    "57ing_key.txt": (
        "pipe",
        PUB,
        "57ing/Sensitive-word combined key list",
        "https://github.com/57ing/Sensitive-word",
    ),
    "fwwdn_political.txt": (
        "txt",
        PUB,
        "fwwdn/sensitive-stop-words political-category file",
        "https://github.com/fwwdn/sensitive-stop-words",
    ),
    "fwwdn_porn.txt": (
        "txt",
        PUB,
        "fwwdn/sensitive-stop-words porn-category file",
        "https://github.com/fwwdn/sensitive-stop-words",
    ),
    "fwwdn_gunsexpl.txt": (
        "txt",
        PUB,
        "fwwdn/sensitive-stop-words guns/explosives file",
        "https://github.com/fwwdn/sensitive-stop-words",
    ),
    "qloog_baidu_filter.txt": (
        "txt",
        PUB,
        "qloog/sensitive_words Baidu filter list",
        "https://github.com/qloog/sensitive_words",
    ),
    "xwg666_noncompliant.txt": (
        "txt",
        PUB,
        "xwg666/Sensitive-words non-compliant list",
        "https://github.com/xwg666/Sensitive-words",
    ),
    "chat_sensitive_words.pkl": (
        "pickle",
        PUB,
        "kaixindelele/ChatSensitiveWords pickle (LLM-platform context)",
        "https://github.com/kaixindelele/ChatSensitiveWords",
    ),
    "selfcs_illegal.txt": (
        "txt",
        PUB,
        "selfcs/stop-and-sensitive-words illegal-category file",
        "https://github.com/selfcs/stop-and-sensitive-words",
    ),
    "gfwlist_domains.txt": (
        "gfwlist",
        PUB,
        "gfwlist/gfwlist (domain/URL patterns — NOT word-level; control group)",
        "https://github.com/gfwlist/gfwlist",
    ),
    # Yesterday's already-fetched lists (data/, see data/SOURCES.md) — local load only, NOT refetched.
    "cn_ai_wordbank.txt": (
        "txt",
        DATA,
        "PRIOR: mimikin/AI-Sensitive-Word-Bank (fetched 2026-08-29)",
        "https://github.com/mimikin/AI-Sensitive-Word-Bank",
    ),
    "ldnoobw_zh.txt": (
        "txt",
        DATA,
        "PRIOR: LDNOOBW zh (fetched 2026-08-29)",
        "https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words",
    ),
    "ldnoobw_en.txt": (
        "txt",
        DATA,
        "PRIOR: LDNOOBW en (fetched 2026-08-29)",
        "https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words",
    ),
    "gpw_zh.txt": (
        "txt",
        DATA,
        "PRIOR: google-profanity-words zh (fetched 2026-08-29)",
        "https://github.com/coffee-and-fun/google-profanity-words",
    ),
    "gpw_en.txt": (
        "txt",
        DATA,
        "PRIOR: google-profanity-words en (fetched 2026-08-29)",
        "https://github.com/coffee-and-fun/google-profanity-words",
    ),
    "profane_en.json": (
        "json",
        DATA,
        "PRIOR: zacanger/profane-words json (fetched 2026-08-29)",
        "https://github.com/zacanger/profane-words",
    ),
}


def analyze(fname, kind, base, desc, url, verified, ver_han, ver_lat, vset, N):
    path = base / fname
    if not path.exists():
        return {"file": fname, "error": "missing"}
    n_rows = None
    try:
        if kind == "csv":
            items, n_rows = load_csv_allcells(path)
        elif kind == "csvkw":
            items, n_rows = load_csv_keyword_col(path)
        elif kind == "gfwlist":
            items = load_gfwlist(path)
        elif kind == "dsplit":
            items = load_dsplit(path)
        elif kind == "pipe":
            items = load_pipe(path)
        elif kind == "pickle":
            items = load_pickle_list(path)
        elif kind == "json":
            items = load_json_list(path)
        else:
            items = load_txt(path)
    except Exception as e:
        return {"file": fname, "error": type(e).__name__}

    entries = {norm(i) for i in items if i and i.strip()}
    eset = entries
    exact = sum(1 for t in verified if t in eset)
    sub = sum(
        1 for t in verified if any(t in e for e in eset)
    )  # verified term ⊆ some entry
    confirmed_exact = sum(1 for e in eset if e in vset)
    confirmed_contains = sum(1 for e in eset if any(v in e for v in vset))
    sub_han = sum(1 for t in ver_han if any(t in e for e in eset))
    sub_lat = sum(1 for t in ver_lat if any(t in e for e in eset))
    han_entries = sum(1 for e in eset if is_han(e))

    return {
        "file": fname,
        "source": desc,
        "url": url,
        "raw_lines_or_rows": n_rows if n_rows is not None else len(items),
        "entries_unique_normalized": len(entries),
        "entries_han": han_entries,
        "verified_set_cjk": len(ver_han),
        "verified_set_latin": len(ver_lat),
        "verified_exact_hits": exact,
        "verified_substring_hits": sub,
        "verified_substring_hits_of_cjk_slice": sub_han,
        "verified_substring_hits_of_latin_slice": sub_lat,
        "pct_of_verified_found": round(100.0 * sub / N, 1) if N else None,
        "pct_of_cjk_slice": round(100.0 * sub_han / len(ver_han), 1)
        if ver_han
        else None,
        "list_entries_matching_verified_exact": confirmed_exact,
        "list_entries_containing_verified_substring": confirmed_contains,
        "pct_of_list_confirmed_exact": round(100.0 * confirmed_exact / len(entries), 2)
        if entries
        else None,
        "_eset": entries,
    }


def main():
    ver = json.loads((ROOT / "results" / "VERIFIED_BLOCKERS_ALL.json").read_text())
    verified = [norm(t) for t in ver["verified_single"]]
    verified = [t for t in verified if t]
    N = len(verified)
    vset = set(verified)
    ver_han = [t for t in verified if is_han(t)]
    ver_lat = [t for t in verified if not is_han(t)]

    results = []
    for fname, (kind, base, desc, url) in LOADERS.items():
        results.append(
            analyze(fname, kind, base, desc, url, verified, ver_han, ver_lat, vset, N)
        )

    # union coverage across ALL lists (today's + prior)
    esets = [r["_eset"] for r in results if "_eset" in r]
    covered = {t for t in verified if any(any(t in e for e in es) for es in esets)}
    covered_exact = {t for t in verified if any(t in es for es in esets)}
    union = {
        "lists_combined": len(esets),
        "verified_found_substring_any_list": len(covered),
        "verified_found_exact_any_list": len(covered_exact),
        "pct_of_verified_union": round(100.0 * len(covered) / N, 1),
        "verified_uncovered_by_any_public_list": N - len(covered),
    }
    for r in results:
        r.pop("_eset", None)

    OUT.write_text(
        json.dumps(
            {"verified_set_size": N, "union_coverage": union, "lists": results},
            indent=2,
            ensure_ascii=False,
        )
    )

    print(f"verified set size: {N}  (cjk {len(ver_han)} / latin-digit {len(ver_lat)})")
    print(
        f"UNION coverage (any list): {union['verified_found_substring_any_list']}/{N} "
        f"({union['pct_of_verified_union']}%), exact-any: {union['verified_found_exact_any_list']}, "
        f"uncovered: {union['verified_uncovered_by_any_public_list']}"
    )
    hdr = (
        f"{'file':38s} {'entries':>9s} {'exact':>6s} {'substr':>7s} {'%560':>6s} "
        f"{'%cjk':>6s} {'conf_ex':>8s} {'conf%':>7s}"
    )
    print(hdr)
    for r in results:
        if "error" in r:
            print(f"{r['file']:38s} ERROR: {r['error']}")
            continue
        print(
            f"{r['file']:38s} {r['entries_unique_normalized']:>9d} {r['verified_exact_hits']:>6d} "
            f"{r['verified_substring_hits']:>7d} {r['pct_of_verified_found']:>6.1f} "
            f"{r['pct_of_cjk_slice']:>6.1f} {r['list_entries_matching_verified_exact']:>8d} "
            f"{r['pct_of_list_confirmed_exact']:>7.2f}"
        )
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    sys.exit(main())
