# -*- coding: utf-8 -*-
"""
GEO tracker report export to a branded Optimize.uz DOCX.

Usage:
  python report_docx.py            → geo_report_DATE.docx
  python report_docx.py --out path

The style mirrors dashboard.html: ink #16181D, accent #FF5C1F,
Space Grotesk headings, Inter body text, JetBrains Mono figures.
"""

import argparse
import csv
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from queries import DOMAINS, NICHES

BASE_DIR = Path(__file__).resolve().parent
RESULTS_CSV = BASE_DIR / "results.csv"

INK = RGBColor(0x16, 0x18, 0x1D)
ACCENT = RGBColor(0xFF, 0x5C, 0x1F)
MUTED = RGBColor(0x6B, 0x71, 0x80)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
ACCENT_SOFT = "FFF0E9"   # w:shd fills take a plain hex string
LINE = "E4E6E0"

F_HEAD = "Space Grotesk"
F_BODY = "Inter"
F_MONO = "JetBrains Mono"


# ---------------------------------------------------------------------------
# Data (same logic as report_html)
# ---------------------------------------------------------------------------

def _hit(row, domain):
    return row.get(f"src_{domain}") == "1" or row.get(f"txt_{domain}") == "1"


def load_ok_rows():
    with RESULTS_CSV.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return rows, [r for r in rows if r["status"] == "ok"]


def sov(rows, *, niche=None, engine=None):
    subset = [r for r in rows
              if (niche is None or r["niche"] == niche)
              and (engine is None or r["engine"] == engine)]
    if not subset:
        return None
    return {d: round(100 * sum(_hit(r, d) for r in subset) / len(subset), 1)
            for d in DOMAINS}


# ---------------------------------------------------------------------------
# Low-level formatting helpers
# ---------------------------------------------------------------------------

def shade(cell, hex_color):
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_color)
    cell._tc.get_or_add_tcPr().append(shd)


def style_run(run, *, font=F_BODY, size=10, bold=False, color=INK):
    run.font.name = font
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color


def para(doc, text="", *, font=F_BODY, size=10, bold=False, color=INK,
         space_after=6, space_before=0):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.space_before = Pt(space_before)
    if text:
        style_run(p.add_run(text), font=font, size=size, bold=bold, color=color)
    return p


def heading(doc, text, *, size=13):
    return para(doc, text, font=F_HEAD, size=size, bold=True,
                space_before=14, space_after=4)


def hairline(paragraph, color=LINE):
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:color"), color)
    pbdr.append(bottom)
    paragraph._p.get_or_add_pPr().append(pbdr)


def make_table(doc, n_rows, n_cols):
    table = doc.add_table(rows=n_rows, cols=n_cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    # repaint the Table Grid borders in the brand hairline color
    tbl_pr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:color"), LINE)
        borders.append(el)
    tbl_pr.append(borders)
    return table


def fill_cell(cell, text, *, font=F_BODY, size=9, bold=False, color=INK,
              fill=None, center=True):
    cell.text = ""
    p = cell.paragraphs[0]
    if center:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    style_run(p.add_run(text), font=font, size=size, bold=bold, color=color)
    if fill:
        shade(cell, fill)


def header_row(table, labels):
    for i, label in enumerate(labels):
        fill_cell(table.rows[0].cells[i], label,
                  font=F_MONO, size=8, bold=True, color=WHITE, fill="16181D")


def pct_cell(cell, value, *, primary=False):
    """Percentage cell: fill intensity by visibility level, marker on the primary."""
    if value >= 50:
        fill, color, bold = "FF5C1F", WHITE, True
    elif value >= 20:
        fill, color, bold = "FFC9B0", INK, True
    elif value > 0:
        fill, color, bold = ACCENT_SOFT, INK, True
    else:
        fill, color, bold = None, MUTED, False
    text = f"{value:.0f}%" + (" ★" if primary else "")
    fill_cell(cell, text, font=F_MONO, size=9, bold=bold, color=color, fill=fill)


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------

def build_docx(out_path: Path) -> Path:
    all_rows, ok = load_ok_rows()
    dates = sorted({r["run_date"] for r in all_rows})
    latest = dates[-1]
    latest_ok = [r for r in ok if r["run_date"] == latest]
    engines = sorted({r["engine"] for r in latest_ok})

    doc = Document()
    for section in doc.sections:
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        section.left_margin = section.right_margin = Cm(2)
        section.top_margin, section.bottom_margin = Cm(1.8), Cm(1.8)

    # Header: Optimize<.>uz GEO monitor, same as the dashboard
    p = doc.add_paragraph()
    style_run(p.add_run("Optimize"), font=F_HEAD, size=22, bold=True)
    style_run(p.add_run("."), font=F_HEAD, size=22, bold=True, color=ACCENT)
    style_run(p.add_run("uz"), font=F_HEAD, size=22, bold=True)
    style_run(p.add_run("  GEO-монитор"), font=F_HEAD, size=22, bold=True)
    hairline(p, color="16181D")
    para(doc,
         f"Отчёт о цитируемости доменов в ответах ИИ · последний прогон {latest} · "
         f"сформирован {datetime.now().strftime('%d.%m.%Y %H:%M')}",
         font=F_MONO, size=8.5, color=MUTED, space_after=12)

    # KPI row
    kpi = make_table(doc, 2, 4)
    for i, (val, label) in enumerate([
            (str(len(latest_ok)), f"ответов в прогоне {latest}"),
            (str(len(engines)), "движков: " + ", ".join(engines)),
            (str(sum(1 for r in latest_ok for d in DOMAINS if _hit(r, d))),
             "попаданий доменов"),
            (f"{len(dates)}", f"дат в истории ({dates[0]} … {latest})")]):
        fill_cell(kpi.rows[0].cells[i], val, font=F_MONO, size=16, bold=True,
                  color=ACCENT if i == 2 else INK)
        fill_cell(kpi.rows[1].cells[i], label, size=8, color=MUTED)

    # Visibility matrix by niche
    heading(doc, "Видимость по нишам")
    para(doc, "Доля запросов ниши, где домен попал в источники (src) или текст "
              "ответа (txt). ★ — целевой домен ниши.",
         size=9, color=MUTED)
    short = {d: d.split(".")[0] for d in DOMAINS}
    m = make_table(doc, len([n for n in NICHES if sov(latest_ok, niche=n)]) + 1,
                   len(DOMAINS) + 1)
    header_row(m, ["ниша"] + [short[d] for d in DOMAINS])
    r_i = 1
    for niche, cfg in NICHES.items():
        by_dom = sov(latest_ok, niche=niche)
        if not by_dom:
            continue
        fill_cell(m.rows[r_i].cells[0], cfg["title"], size=9, bold=True, center=False)
        for c_i, d in enumerate(DOMAINS, start=1):
            pct_cell(m.rows[r_i].cells[c_i], by_dom[d],
                     primary=d == cfg["primary_domain"])
        r_i += 1

    # Overall leaderboard
    heading(doc, "Общий зачёт по доменам")
    total = sov(latest_ok) or {}
    lb = sorted(total.items(), key=lambda x: -x[1])
    t = make_table(doc, len(lb) + 1, 3)
    header_row(t, ["домен", "SOV", "комментарий"])
    for i, (d, pct) in enumerate(lb, start=1):
        niches_hit = sorted({r["niche"] for r in latest_ok if _hit(r, d)})
        note = ("ниши: " + ", ".join(NICHES[n]["title"] for n in niches_hit)
                if niches_hit else "не цитируется")
        fill_cell(t.rows[i].cells[0], d, font=F_MONO, size=9, center=False)
        pct_cell(t.rows[i].cells[1], pct)
        fill_cell(t.rows[i].cells[2], note, size=8.5,
                  color=INK if niches_hit else MUTED, center=False)

    # Per-engine breakdown
    heading(doc, "Сравнение движков")
    e_rows = [e for e in engines if sov(latest_ok, engine=e)]
    et = make_table(doc, len(e_rows) + 1, len(DOMAINS) + 2)
    header_row(et, ["движок", "n"] + [short[d] for d in DOMAINS])
    for i, e in enumerate(e_rows, start=1):
        sub_n = sum(1 for r in latest_ok if r["engine"] == e)
        by_dom = sov(latest_ok, engine=e)
        fill_cell(et.rows[i].cells[0], e, font=F_MONO, size=9, bold=True, center=False)
        fill_cell(et.rows[i].cells[1], str(sub_n), font=F_MONO, size=9, color=MUTED)
        for c_i, d in enumerate(DOMAINS, start=2):
            pct_cell(et.rows[i].cells[c_i], by_dom[d])

    # Auto-derived findings from the data
    heading(doc, "Ключевые наблюдения")
    consensus = []
    single = []
    for d in DOMAINS:
        for niche in NICHES:
            who = {r["engine"] for r in latest_ok
                   if r["niche"] == niche and _hit(r, d)}
            if len(who) == len(engines) > 1:
                consensus.append(f"{d} в нише «{NICHES[niche]['title']}» — "
                                 f"цитируют все движки")
            elif len(who) == 1:
                single.append(f"{d} в нише «{NICHES[niche]['title']}» — "
                              f"только {next(iter(who))}")
    zeros = [d for d in DOMAINS if not any(_hit(r, d) for r in latest_ok)]
    for line in consensus:
        para(doc, "✓ " + line, size=9.5, space_after=3)
    for line in single:
        para(doc, "◐ " + line, size=9.5, color=MUTED, space_after=3)
    if zeros:
        para(doc, "✗ Не цитируются ни одним движком: " + ", ".join(zeros),
             size=9.5, bold=True, color=ACCENT, space_after=3)

    # Hits from the latest run
    heading(doc, "Запросы с попаданиями (последний прогон)")
    hits = [(r, [d for d in DOMAINS if _hit(r, d)])
            for r in latest_ok if any(_hit(r, d) for d in DOMAINS)]
    if hits:
        ht = make_table(doc, len(hits) + 1, 4)
        header_row(ht, ["движок", "ниша", "запрос", "домены"])
        for i, (r, ds) in enumerate(hits, start=1):
            fill_cell(ht.rows[i].cells[0], r["engine"], font=F_MONO, size=8.5,
                      center=False)
            fill_cell(ht.rows[i].cells[1], r["niche"], size=8.5, center=False)
            fill_cell(ht.rows[i].cells[2], r["query"], size=8.5, center=False)
            fill_cell(ht.rows[i].cells[3], ", ".join(ds), font=F_MONO, size=8.5,
                      color=ACCENT, center=False, fill=ACCENT_SOFT)
    else:
        para(doc, "Попаданий нет.", size=9.5, color=MUTED)

    # Footer
    p = para(doc, "", space_before=16)
    hairline(p)
    para(doc, "Optimize.uz · GEO-трекер · только официальные API (Gemini, "
              "Perplexity, OpenAI). ИИ недетерминирован: смотрите тренд по "
              "нескольким прогонам, а не единичный замер.",
         font=F_MONO, size=8, color=MUTED)

    doc.save(out_path)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="Экспорт отчёта в DOCX Optimize.uz")
    parser.add_argument("--out", help="путь к выходному .docx")
    args = parser.parse_args()
    if not RESULTS_CSV.exists():
        raise SystemExit("results.csv не найден — сначала: python geo_tracker.py run")
    default = BASE_DIR / f"geo_report_{datetime.now().strftime('%Y-%m-%d')}.docx"
    out = build_docx(Path(args.out) if args.out else default)
    print(f"Готово: {out}")


if __name__ == "__main__":
    main()
