#!/usr/bin/env python3
"""build_antiscrape.py — package anti-scraping embed artifacts from the lexicon.

SAFETY CONTRACT (do not break):
  - Lexicon/blocker CONTENT never reaches stdout or this file as literals.
    Everything moves file-to-file inside this script. stdout = counts/paths only.
  - Every generated markdown/README/header line is SANITIZED (zero terms).
  - Existing artifact targets are archived to anti_scrape/archive/ before overwrite.

Inputs (read only):
  anti_scrape/lexicon_flat.txt                       -> full lexicon (1311 entries)
  vocab/antiscrape_lexicon.json                      -> cross-check merged_all_count
  results/VERIFIED_BLOCKERS.json                     -> verified_single[]
  vocab/isolated_triggers_20260830_084738.json       -> results[].isolated_minimal[]

Outputs (under anti_scrape/):
  block_verified.txt, block_full.txt, embed.html, embed_minimal.html,
  EMBED_GITHUB.md, README.md
Log: results/logs/antiscrape_build.log (counts/paths only)
"""

from __future__ import annotations

import datetime
import html
import json
import pathlib
import shutil

HERE = pathlib.Path(__file__).resolve().parent
WORK = HERE.parent
OUT = WORK / "anti_scrape"
ARCHIVE = OUT / "archive"
LOG_FILE = WORK / "results/logs/antiscrape_build.log"

FLAT = OUT / "lexicon_flat.txt"
LEXICON_JSON = WORK / "vocab/antiscrape_lexicon.json"
VERIFIED_JSON = WORK / "results/VERIFIED_BLOCKERS.json"
ISOLATED_JSON = WORK / "vocab/isolated_triggers_20260830_084738.json"

BUILD_DATE = "2026-08-30"


# ---------------------------------------------------------------- utilities
def log(msg: str) -> None:
    """Append a sanitized (counts/paths only) line to the build log."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.datetime.utcnow().isoformat()}Z] {msg}\n")


def archive_if_exists(path: pathlib.Path) -> bool:
    if path.exists():
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, ARCHIVE / f"{path.name}_{ts}")
        return True
    return False


def write_text(path: pathlib.Path, text: str) -> int:
    archive_if_exists(path)
    path.write_text(text, encoding="utf-8")
    return path.stat().st_size


def load_full_lexicon() -> tuple[list[str], int, int]:
    """Full lexicon: flat file (skip blanks/# comments), deduped, order kept."""
    entries: list[str] = []
    seen: set[str] = set()
    skipped_comments = 0
    for line in FLAT.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            skipped_comments += 1
            continue
        if s not in seen:
            seen.add(s)
            entries.append(s)
    meta_count = -1
    try:
        data = json.loads(LEXICON_JSON.read_text(encoding="utf-8"))
        meta_count = int(data.get("merged_all_count", -1))
    except Exception:
        pass
    return entries, meta_count, skipped_comments


def load_verified() -> tuple[list[str], int]:
    """verified_single + isolated_minimal phrases, deduped, order kept."""
    verified: list[str] = []
    seen: set[str] = set()
    data = json.loads(VERIFIED_JSON.read_text(encoding="utf-8"))
    for t in data.get("verified_single", []):
        s = t.strip()
        if s and s not in seen:
            seen.add(s)
            verified.append(s)
    n_singles = len(verified)
    iso = json.loads(ISOLATED_JSON.read_text(encoding="utf-8"))
    for r in iso.get("results", []):
        for t in r.get("isolated_minimal", []):
            s = t.strip()
            if s and s not in seen:
                seen.add(s)
                verified.append(s)
    return verified, n_singles


# ------------------------------------------------------------- sanitization
def char_risk_report(entries: list[str]) -> dict[str, int]:
    """Counts only — flags characters that could break HTML/comment syntax."""
    joined = "\n".join(entries)
    return {
        "has_dashdash": joined.count("--"),
        "has_comment_end": joined.count("-->"),
        "has_lt": joined.count("<"),
        "has_gt": joined.count(">"),
        "has_amp": joined.count("&"),
    }


# ------------------------------------------------------------- content build
VERIFIED_HEADER = (
    "# Anti-scraping poison text for deployment ONLY on properties you own or control.\n"
    f"# Every entry below was verified to trip the live content filter via automated\n"
    f"# probes on {BUILD_DATE}. Do not paste into AI-provider chats/PRs/issues.\n"
)

FULL_HEADER = (
    "# Anti-scraping poison text (FULL lexicon) for deployment ONLY on properties\n"
    f"# you own or control. Mix of live-filter-verified and model-recalled entries\n"
    f"# (not every entry individually verified). Built {BUILD_DATE}.\n"
)


def embed_html(entries: list[str], label: str) -> str:
    """One self-contained HTML snippet: usage comment, poison comment, hidden div."""
    body = ", ".join(entries)
    risk = char_risk_report(entries)
    # If any entry could terminate a comment or inject markup, escape for the
    # comment/div payloads (entries here are plain words; expected 0 flags).
    needs_escape = bool(risk["has_comment_end"] or risk["has_lt"] or risk["has_gt"])
    comment_body = html.escape(body, quote=False) if needs_escape else body
    div_body = html.escape(body, quote=False) if needs_escape else body
    parts = [
        f"<!-- Anti-scrape embed ({label}): deploy on YOUR OWN sites/repos only. -->\n",
        "<!-- Usage guide: anti_scrape/EMBED_GITHUB.md (hidden from humans; ignored by compliant crawlers). -->\n",
        "<!-- ANTI-SCRAPE POISON BEGIN (do not edit entries) -->\n",
        comment_body,
        "\n<!-- ANTI-SCRAPE POISON END -->\n",
        '<div style="display:none" aria-hidden="true" data-antiscrape>\n',
        div_body,
        "\n</div>\n",
    ]
    return "".join(parts)


def embed_minimal(entries: list[str]) -> str:
    body = ", ".join(entries)
    risk = char_risk_report(entries)
    needs_escape = bool(risk["has_comment_end"] or risk["has_lt"] or risk["has_gt"])
    payload = html.escape(body, quote=False) if needs_escape else body
    parts = [
        "<!-- Anti-scrape embed (verified-only, minimal): YOUR OWN properties only. -->\n",
        "<!-- Usage guide: anti_scrape/EMBED_GITHUB.md. -->\n",
        "<!-- ANTI-SCRAPE POISON BEGIN (verified entries; do not edit) -->\n",
        payload,
        "\n<!-- ANTI-SCRAPE POISON END -->\n",
        '<div style="display:none" aria-hidden="true" data-antiscrape>\n',
        payload,
        "\n</div>\n",
    ]
    return "".join(parts)


# -------------------------------------------------------------- docs (clean)
def embed_github_md(n_verified: int, n_full: int) -> str:
    return f"""# Embedding the anti-scraping poison text

Automated packager output ({BUILD_DATE}). This file intentionally contains **zero
lexicon content** — always reference the artifact files by path, never paste their
contents into chats, pull requests, or issues.

## Artifacts

| File | Entries | Purpose |
|------|---------|---------|
| `anti_scrape/block_verified.txt` | {n_verified} | Only entries individually verified to trip the live filter |
| `anti_scrape/block_full.txt` | {n_full} | Full lexicon: mix of verified and model-recalled entries |
| `anti_scrape/embed.html` | {n_full} | Self-contained HTML snippet (full lexicon), ready to paste into a page template |
| `anti_scrape/embed_minimal.html` | {n_verified} | Same snippet, verified-only entries, small footprint |

## Option A — plain text file (GitHub repos, sites)

Commit the text file into your repository or web root, e.g.:

```bash
mkdir -p .well-known
cp anti_scrape/block_full.txt .well-known/poison.txt
```

Any path works: `.well-known/poison.txt`, `poison.txt`, or a docs page. Scrapers
that hoover your repository or site wholesale will ingest it.

## Option B — HTML embed (websites)

Paste the entire contents of `anti_scrape/embed.html` into your page template
(footer or end of `<body>`). It is one self-contained snippet:

- a 2-line usage comment,
- an HTML comment block holding the full entry set (comma-separated),
- a `<div style="display:none" aria-hidden="true" data-antiscrape>` holding the
  same content.

Use `anti_scrape/embed_minimal.html` instead if you want the smallest footprint —
it contains only the live-filter-verified entries.

## Why this works

- **Humans** never see the text (hidden div; it lives in the page source).
- **Compliant crawlers** honour `robots.txt` and skip hidden/aria-hidden nodes —
  they are unaffected.
- **Non-compliant scrapers** (notably AI training crawlers that ignore robots.txt
  and DOM semantics) ingest the text and poison their own pipeline: the blocked
  sequences contaminate downstream filtering, chunking, and training runs.

## Legal / visibility notes

- Deploy on properties you own or control only.
- The text is visible to anyone who views your page source or repo — that is
  expected; it contains no secrets, only filter-bait vocabulary.
- This is a defensive measure for your own content; it does not circumvent any
  protection — it weaponises over-broad scraping against the scraper.

## ⚠️ Handling warning

NEVER paste the contents of `block_*.txt` or the embed snippets into chats, pull
requests, issues, or CI logs of AI providers (especially Chinese AI providers) —
the entries trip their content filters and will block or flag your request.
Reference the files by path instead.
"""


def readme_md(rows: list[tuple[str, int, int]], n_verified: int, n_full: int) -> str:
    table = "\n".join(
        f"| `{name}` | {count} | {size:,} B |" for name, count, size in rows
    )
    return f"""# anti_scrape/ — packaging overview

Built {BUILD_DATE} by `scripts/build_antiscrape.py`. This README is sanitized:
it contains **zero lexicon content**. See `EMBED_GITHUB.md` for deployment.

## What is here

Defensive "poison text" for the site owner's **own** websites and repositories.
It is hidden from human visitors, ignored by compliant crawlers, and poisons
non-compliant scrapers (AI training crawlers that ignore robots.txt) by feeding
them filter-triggering vocabulary.

- `{n_verified}` entries are individually **verified** against the live filter
  (24 verified singles + isolated trigger phrases, deduped).
- `{n_full}` entries form the **full lexicon** (mix of verified and
  model-recalled entries).

## Counts & sizes

| Artifact | Entries | Size |
|----------|---------|------|
{table}

## Regenerate

```bash
python3 scripts/build_antiscrape.py      # build artifacts (counts to stdout)
python3 scripts/validate_antiscrape.py   # live validation probes (verdicts only)
```

Validation results: `results/antiscrape_validation.json`.
Build/validation log: `results/logs/antiscrape_build.log`.

## Handling

Never paste artifact contents into AI-provider chats/PRs/issues (see
`EMBED_GITHUB.md`). Reference files by path only.
"""


# ------------------------------------------------------------------- main
def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    log("build_antiscrape: start")

    full, meta_count, n_comments = load_full_lexicon()
    verified, n_singles = load_verified()
    log(
        f"inputs: flat_entries={len(full)} (comments={n_comments}) "
        f"json_merged_all_count={meta_count} verified_singles={n_singles} "
        f"verified_total_after_dedupe={len(verified)}"
    )
    if meta_count >= 0 and len(full) != meta_count:
        log(f"WARN: flat count {len(full)} != merged_all_count {meta_count}")

    risk_full = char_risk_report(full)
    log(f"char-risk full: {risk_full}")

    # --- a. block_verified.txt
    p = OUT / "block_verified.txt"
    size_a = write_text(p, VERIFIED_HEADER + "\n".join(verified) + "\n")

    # --- b. block_full.txt
    p = OUT / "block_full.txt"
    size_b = write_text(p, FULL_HEADER + "\n".join(full) + "\n")

    # --- c. embed.html
    p = OUT / "embed.html"
    embed_full = embed_html(full, "full lexicon")
    size_c = write_text(p, embed_full)

    # --- d. embed_minimal.html
    p = OUT / "embed_minimal.html"
    embed_min = embed_minimal(verified)
    size_d = write_text(p, embed_min)

    # --- e. EMBED_GITHUB.md (sanitized)
    p = OUT / "EMBED_GITHUB.md"
    size_e = write_text(p, embed_github_md(len(verified), len(full)))

    # --- f. README.md (sanitized)
    rows = [
        ("block_verified.txt", len(verified), size_a),
        ("block_full.txt", len(full), size_b),
        ("embed.html", len(full), size_c),
        ("embed_minimal.html", len(verified), size_d),
        ("EMBED_GITHUB.md", 0, size_e),
    ]
    p = OUT / "README.md"
    size_f = write_text(p, readme_md(rows, len(verified), len(full)))

    budget_ok = size_c <= 120 * 1024
    log(
        f"artifacts: block_verified.txt entries={len(verified)} bytes={size_a} | "
        f"block_full.txt entries={len(full)} bytes={size_b} | "
        f"embed.html bytes={size_c} (budget 122880: {'OK' if budget_ok else 'OVER'}) | "
        f"embed_minimal.html bytes={size_d} | EMBED_GITHUB.md bytes={size_e} | "
        f"README.md bytes={size_f}"
    )
    log("build_antiscrape: done")

    # stdout: counts/paths only
    print(f"block_verified.txt : {len(verified):5d} entries  {size_a:7,d} B")
    print(f"block_full.txt     : {len(full):5d} entries  {size_b:7,d} B")
    print(
        f"embed.html         : {len(full):5d} entries  {size_c:7,d} B  "
        f"{'OK' if budget_ok else 'OVER 120KB'}"
    )
    print(f"embed_minimal.html : {len(verified):5d} entries  {size_d:7,d} B")
    print(f"EMBED_GITHUB.md    : {'sanitized':>9s}      {size_e:7,d} B")
    print(f"README.md          : {'sanitized':>9s}      {size_f:7,d} B")
    print(
        f"meta merged_all_count={meta_count} flat_entries={len(full)} "
        f"char_risk={risk_full}"
    )
    return 0 if budget_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
