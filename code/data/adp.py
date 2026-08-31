"""Parse the locked FFC 2QB ADP PDF snapshot into data/adp_2026.csv.

The PDF is a print-to-PDF of https://fantasyfootballcalculator.com/adp/2qb
(12 teams, all players) captured 8/23/26 11:44 PM — the ADP lock for keeper costs.

pypdf extracts each page as: numeric stat rows first, then the player names in
the same order. Stat row fields are glued together; decimals disambiguate:
    "26 RBPHI1025.55.31.074.041477"
     rank=26 pos=RB team=PHI bye=10 adp=25.5 std=5.3 high=1.07 low=4.04 n=1477
"""

import csv
import re
from pathlib import Path

import pypdf

from robo import DATA, ROOT

PDF = ROOT / "2 QB Average Draft Position (2026), 12 Teams All Players.pdf"
OUT = DATA / "adp_2026.csv"

# The bye week and ADP are glued together with no separator ("...DAL1425.7..." can
# read as bye 14 / adp 25.7 or bye 1 / adp 425.7). Try both greedy and lazy bye
# splits and keep whichever ADP is consistent with the ascending rank order.
def _row_re(bye_quant: str) -> re.Pattern:
    return re.compile(
        r"^(?P<rank>\d{1,3})\s+"
        r"(?P<pos>QB|RB|WR|TE|DEF|PK)"
        r"(?P<team>[A-Z]{2,3})\s*"
        rf"(?P<bye>\d{{1,2}}{bye_quant})\s*"
        r"(?P<adp>[1-9]\d{0,2}\.\d)\s*"
        r"(?P<std>\d{1,2}\.\d)\s*"
        r"(?P<high>\d{1,2}\.\d{2})\s*"
        r"(?P<low>\d{1,2}\.\d{2})\s*"
        r"(?P<times>\d+)$"
    )


ROW_RES = [_row_re(""), _row_re("?")]  # greedy bye first, then lazy
ROW_RE = ROW_RES[0]  # any-match test


def match_stat_line(line: str, prev_adp: float) -> dict | None:
    candidates = []
    for rx in ROW_RES:
        m = rx.match(line)
        if m:
            d = m.groupdict()
            if d not in candidates:
                candidates.append(d)
    if not candidates:
        return None
    plausible = [c for c in candidates if float(c["adp"]) >= prev_adp - 0.05]
    pool = plausible or candidates
    return min(pool, key=lambda c: float(c["adp"]))

SKIP_PREFIXES = (
    "https://", "Page ", "# Pos", "Dev", "Drafted", "Name", "2 QB", "2026",
    "10 Teams", "12 Teams", "All", " QB", " RB", " WR", " TE", " DEF", " PK",
    "List", " Draftboard", "CSV", " JSON", "Get a printable", "unlimited",
    "UPGRADE", "Data from", "More info",
)


def parse_pdf(pdf_path: Path = PDF) -> list[dict]:
    reader = pypdf.PdfReader(pdf_path)
    rows: list[dict] = []
    # names can spill onto the next page's name block, so pair globally:
    # stat rows and names each appear in rank order across the whole document
    stats, names = [], []
    prev_adp = 0.0
    for page in reader.pages:
        page_stats, page_names = [], []
        for line in page.extract_text().splitlines():
            line = line.strip()
            if not line or any(line.startswith(p) for p in SKIP_PREFIXES):
                continue
            if line == "Teams All Players" or re.fullmatch(r"([A-Za-z ]+)\1", line):
                continue  # print-view furniture (doubled labels like "QBQB")
            m = match_stat_line(line, prev_adp)
            if m:
                page_stats.append(m)
                prev_adp = float(m["adp"])
            elif re.match(r"^\d", line):
                continue  # numeric junk that didn't match (page furniture)
            elif line in ("here.", "more.") or len(line) > 32:
                continue  # explainer prose on the last content page
            else:
                page_names.append(line)
        if stats and not page_stats:
            break  # end of the ranking table; rest is site-footer prose
        stats.extend(page_stats)
        names.extend(page_names)
    if len(stats) != len(names):
        raise ValueError(f"parse mismatch: {len(stats)} stat rows vs {len(names)} names")
    for s, name in zip(stats, names):
        rows.append({
                "rank": int(s["rank"]),
                "name": name,
                "pos": s["pos"],
                "team": s["team"],
                "bye": int(s["bye"]),
                "adp": float(s["adp"]),
                "high": s["high"],
                "low": s["low"],
                "times_drafted": int(s["times"]),
            })
    # sanity: ranks contiguous, adp non-decreasing (catches bye/adp glue misparse)
    rows.sort(key=lambda r: r["rank"])
    for i, r in enumerate(rows, 1):
        if r["rank"] != i:
            raise ValueError(f"rank gap at {i}: got {r['rank']} ({r['name']})")
    for a, b in zip(rows, rows[1:]):
        if b["adp"] < a["adp"] - 0.05:
            raise ValueError(f"ADP not monotonic: {a['name']} {a['adp']} -> {b['name']} {b['adp']}")
    return rows


def write_csv(rows: list[dict], out: Path = OUT) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def load(out: Path = OUT) -> list[dict]:
    with out.open(encoding="utf-8") as f:
        return [
            {**r, "rank": int(r["rank"]), "bye": int(r["bye"]), "adp": float(r["adp"])}
            for r in csv.DictReader(f)
        ]


if __name__ == "__main__":
    rows = parse_pdf()
    write_csv(rows)
    print(f"parsed {len(rows)} players -> {OUT}")
    for r in rows[:5] + rows[-3:]:
        print(f"  {r['rank']:>3} {r['name']:<28} {r['pos']:<3} {r['team']:<3} adp={r['adp']}")
