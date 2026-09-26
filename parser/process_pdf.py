import csv
import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Callable

import pdfplumber
from PyPDF2 import PdfReader, PdfWriter

from paths.config import SPLIT_PDF_DIR
import pandas as pd

# Splitting

def split_pdf(input_pdf: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    reader = PdfReader(input_pdf)

    for page_num, page in enumerate(reader.pages, start=1):
        writer = PdfWriter()
        writer.add_page(page)

        output_file = output_dir / f"page_{page_num}.pdf"

        with open(output_file, "wb") as f:
            writer.write(f)


def _resolve_pdf(path: Path) -> Path:
    """Accept a .pdf file path, or a folder containing exactly one PDF."""
    path = Path(path)
    if path.is_file():
        return path
    if path.is_dir():
        pdfs = sorted(path.glob("*.pdf"))
        if not pdfs:
            raise FileNotFoundError(f"No PDF files found in folder: {path}")
        if len(pdfs) > 1:
            raise ValueError(
                f"{len(pdfs)} PDFs found in {path}. Pass a single .pdf file, "
                f"or loop over them with split_pdf_to_folder()."
            )
        return pdfs[0]
    raise FileNotFoundError(f"Path does not exist: {path}")


def split_pdf_to_folder(raw_pdf_path: Path) -> Path:
    """
    Splits a PDF into individual page PDFs.

    Args:
        raw_pdf_path: Path to the source PDF, or a folder containing one PDF.

    Returns:
        Path to the output directory containing the split pages.
    """
    raw_pdf_path = _resolve_pdf(raw_pdf_path)

    output_dir = SPLIT_PDF_DIR / raw_pdf_path.stem

    split_pdf(raw_pdf_path, output_dir)

    return output_dir


# --------------------------------------------------------------------------- #
# Parsing  (PDF table -> rows)
# --------------------------------------------------------------------------- #
FULLCODE = re.compile(r"(?<!\d)\d{15}(?!\d)")

CSV_COLUMNS = [
    "county_code",
    "county_name",
    "const_code",
    "const_name",
    "caw_code",
    "caw_name",
    "reg_centre_code",
    "reg_centre_name",
    "polling_station_code",
    "polling_station_seq",
    "polling_station_name",
    "registered_voters",
]


# --------------------------------------------------------------------------- #
# 2022 layout
# --------------------------------------------------------------------------- #
def page_lines_2022(page) -> list[str]:
    return (page.extract_text() or "").split("\n")


def parse_line_2022(line: str) -> dict | None:
    """
    Parse a single text line into a row dict.

    Returns None for non-data lines (title, header, 'Page x of y', 'Total ...').
    Returns a dict with an '_error' key if the line looks like data but the
    fields could not be separated.

    Strategy: the 15-digit polling-station code is the anchor. We split the line
    ON that code, then parse what comes before it (the four code/name pairs) and
    what comes after it (polling-station name + voter count). Splitting on the
    code avoids depending on whitespace around it, which the PDF text extractor
    sometimes drops (e.g. '...MUTETHANIA014063031204901...').
    """
    m = FULLCODE.search(line)
    if not m:
        return None
    full = m.group(0)
    before = line[:m.start()].rstrip()
    after = line[m.end():].strip()

    cty, con, caw, rc, seq = (
        full[:3], full[3:6], full[6:10], full[10:13], full[13:15],
    )

    # Left side: "<cty>county <con>const <caw>caw <rc>rcentre"
    bm = re.match(
        rf"^\s*{cty}(?P<county>.+?)\s+"
        rf"{con}(?P<const>.+?)\s+"
        rf"{caw}(?P<caw>.+?)\s+"
        rf"{rc}(?P<rcentre>.+)$",
        before,
    )
    # Right side: "<polling-station name> <voters>"  (voters may contain stray
    # spaces, e.g. '6 21' -> 621, due to extraction artifacts)
    am = re.match(r"^(?P<ps>.*?)\s*(?P<voters>[\d ]+)$", after)

    if not bm or not am:
        return {"_error": line}

    voters = am.group("voters").replace(" ", "")

    rcentre_name = bm.group("rcentre").strip()
    ps_name = am.group("ps").strip()
    # The extractor sometimes explodes the trailing name into single letters
    # (e.g. 'S C H O O L'). When the polling-station name is just the spaced-out
    # version of the reg-centre name, prefer the clean reg-centre spelling.
    if ps_name.replace(" ", "").upper() == rcentre_name.replace(" ", "").upper():
        ps_name = rcentre_name

    return {
        "county_code": cty,
        "county_name": bm.group("county").strip(),
        "const_code": con,
        "const_name": bm.group("const").strip(),
        "caw_code": caw,
        "caw_name": bm.group("caw").strip(),
        "reg_centre_code": rc,
        "reg_centre_name": rcentre_name,
        "polling_station_code": full,
        "polling_station_seq": seq,
        "polling_station_name": ps_name,
        "registered_voters": int(voters) if voters else None,
    }


parse_line = parse_line_2022  # backwards-compatible alias


# --------------------------------------------------------------------------- #
# 2017 layout
# --------------------------------------------------------------------------- #
# Columns: COUNTY_CODE | COUNTY_NAME | CONST_CODE | CONSTITUENCY_NAME |
#          CAW_CODE | CAW_NAME | POLLING_STATION_CODE | POLLING_CENTER_NAME |
#          CODE (14) | VOTERS | STREAM
#
# extract_text() is unusable here: long names overflow their ruled cells and
# pdfplumber's x-sorted extraction interleaves the glyphs with the next cell
# (e.g. 'E0H0A10B0IL2I0T0A0T6IO01N0 1S'). The content stream draws each cell
# contiguously, so we walk page.chars in stream order and start a new segment
# when x moves backwards (overflow ended) or jumps forward by > GAP. Abutting
# cells ('MOMBASA'+'001') stay merged and are split by ROW_2017, where the
# fixed-width numeric codes are the anchors.
#
# CODE (14 digits) = cty+con+caw+stream+stream. It isn't output; it's only a
# per-row checksum.
_GAP_2017 = 3.0   # pt; spaces are real glyphs in this PDF
_SEP = "\x1f"


def _row_segments(chars: list[dict]) -> list[str]:
    segs, cur, prev = [], [], None
    for ch in chars:
        if prev is not None and (
            ch["x0"] < prev["x1"] - 0.5 or ch["x0"] - prev["x1"] > _GAP_2017
        ):
            segs.append(cur)
            cur = []
        cur.append(ch)
        prev = ch
    if cur:
        segs.append(cur)
    return ["".join(c["text"] for c in s).strip() for s in segs]


def page_lines_2017(page) -> list[str]:
    """One string per visual row, cell segments joined by _SEP."""
    rows: dict[int, list[dict]] = defaultdict(list)
    for ch in page.chars:                      # content-stream order
        rows[round(ch["top"])].append(ch)
    return [_SEP.join(_row_segments(chs)) for _, chs in sorted(rows.items())]


_S = f"{_SEP}?"
_N = f"([^{_SEP}]+?)"
ROW_2017 = re.compile(
    rf"^(\d{{3}}){_S}{_N}{_S}(\d{{3}}){_S}{_N}{_S}(\d{{4}}){_S}{_N}{_S}"
    rf"(\d{{3}}){_S}{_N}{_S}(\d{{14}}){_S}(\d+){_S}(\d{{2}})$"
)


def parse_line_2017(line: str) -> dict | None:
    if not re.match(r"^\d{3}", line):          # title / header rows
        return None
    printable = line.replace(_SEP, " | ")
    m = ROW_2017.match(line)
    if not m:
        return {"_error": printable}
    (cty, county, con, const, caw, caw_name,
     ps_code, centre_name, code14, voters, seq) = m.groups()
    if code14 != cty + con + caw + seq + seq:  # row checksum
        return {"_error": printable}
    return {
        "county_code": cty,                                   # COUNTY_CODE
        "county_name": county.strip(),                        # COUNTY_NAME
        "const_code": con,                                    # CONST_CODE
        "const_name": const.strip(),                          # CONSTITUENCY_NAME
        "caw_code": caw,                                      # CAW_CODE
        "caw_name": caw_name.strip(),                         # CAW_NAME
        "reg_centre_code": None,                              # NULL
        "reg_centre_name": None,                              # NULL
        "polling_station_code": ps_code,                      # POLLING_STATION_CODE
        "polling_station_seq": seq,                           # STREAM
        "polling_station_name": re.sub(r"\s+", " ", centre_name).strip(),  # POLLING_CENTER_NAME
        "registered_voters": int(voters),                     # VOTERS
    }


# layout -> (page -> lines, line -> row dict | None)
LAYOUTS: dict[str, tuple[Callable, Callable]] = {
    "2022": (page_lines_2022, parse_line_2022),
    "2017": (page_lines_2017, parse_line_2017),
}


def parse_pdf(pdf_path: Path, layout: str = "2022") -> tuple[list[dict], list[str]]:
    """Parse one page PDF. Returns (rows, unparsed_lines)."""
    get_lines, parse = LAYOUTS[layout]
    rows: list[dict] = []
    errors: list[str] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for line in get_lines(page):
                rec = parse(line)
                if rec is None:
                    continue
                if "_error" in rec:
                    errors.append(rec["_error"])
                else:
                    rows.append(rec)
            page.close()   # release per-page cache (matters for unsplit PDFs)

    return rows, errors


# --------------------------------------------------------------------------- #
# Folder -> CSV
# --------------------------------------------------------------------------- #
def _page_number(pdf_path: Path) -> int:
    """Sort key so page_2 comes before page_10 (numeric, not lexicographic)."""
    m = re.search(r"(\d+)", pdf_path.stem)
    return int(m.group(1)) if m else 0


def _write_csv(rows: list[dict], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _setup_logger(log_file: Path) -> logging.Logger:
    """Configure a logger that writes to `log_file` and to the console."""
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("pdf_parser")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()      # avoid duplicate handlers if called repeatedly
    logger.propagate = False

    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    )
    logger.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    return logger


def transform_folder_to_csv(
    pdf_folder: Path,
    output_dir: Path,
    log_file: Path | None = None,
    layout: str = "2022",
) -> Path:
    """
    Parse each page PDF in a folder and write one CSV per page.

    The input folder is named after the source PDF (e.g. 'name' for name.pdf).
    A matching sub-folder is created inside output_dir and the per-page CSVs
    are written there:

        pdf_folder = .../split/name/page_1.pdf
        output_dir = .../csv
        result     = .../csv/name/page_1.csv

    A run log is also written (default: <output sub-folder>/parse.log). Rows that
    could not be parsed are logged at WARNING level with their page and text, so
    you can find every problem row with:  grep WARNING parse.log

    Args:
        pdf_folder: Folder named after the source PDF, holding page_*.pdf files.
        output_dir: Parent folder under which a '<name>' sub-folder is created.
        log_file: Optional path for the log file. Defaults to 'parse.log' inside
            the output sub-folder.
        layout: Source layout, "2022" or "2017" (see LAYOUTS).

    Returns:
        Path to the created '<name>' output sub-folder.
    """
    if layout not in LAYOUTS:
        raise ValueError(f"unknown layout {layout!r}; expected one of {list(LAYOUTS)}")

    pdf_folder = Path(pdf_folder)
    output_dir = Path(output_dir) / pdf_folder.name   # csv/<name>/
    output_dir.mkdir(parents=True, exist_ok=True)

    if log_file is None:
        log_file = output_dir / "parse.log"
    logger = _setup_logger(Path(log_file))

    pdf_files = sorted(pdf_folder.glob("*.pdf"), key=_page_number)
    logger.info("found %d PDF(s) in %s (layout=%s)", len(pdf_files), pdf_folder, layout)
    if not pdf_files:
        logger.warning("nothing to do. Are the page_*.pdf files directly inside "
                       "this folder (not in a sub-folder)?")
        return output_dir

    grand_total = 0
    unparsed_total = 0
    voters_total = 0
    for pdf_path in pdf_files:
        rows, errors = parse_pdf(pdf_path, layout)
        output_csv = output_dir / f"{pdf_path.stem}.csv"   # page_1.pdf -> page_1.csv
        _write_csv(rows, output_csv)
        grand_total += len(rows)
        voters_total += sum(r["registered_voters"] or 0 for r in rows)
        logger.info("%s -> %s  (%d rows)", pdf_path.name, output_csv.name, len(rows))
        for line in errors:
            unparsed_total += 1
            logger.warning("unparsed [%s]: %r", pdf_path.name, line)

    logger.info("DONE: %d rows, %d registered voters across %d file(s), "
                "%d unparsed -> %s", grand_total, voters_total, len(pdf_files),
                unparsed_total, output_dir)
    logger.info("log written to %s", log_file)

    for handler in logger.handlers:
        handler.close()
    return output_dir



def combine_csvs(csv_folder: Path, output_csv: Path | None = None) -> Path:
    """Concatenate page_*.csv in page order into one CSV, preserving code zeros."""
    csv_folder = Path(csv_folder)
    files = sorted(csv_folder.glob("page_*.csv"), key=_page_number)
    if not files:
        raise FileNotFoundError(f"no page_*.csv files in {csv_folder}")

    dtypes = {c: "string" for c in CSV_COLUMNS}
    dtypes["registered_voters"] = "Int64"          # nullable int

    df = pd.concat((pd.read_csv(f, dtype=dtypes) for f in files), ignore_index=True)

    output_csv = Path(output_csv or csv_folder.parent / f"{csv_folder.name}.csv")
    df.to_csv(output_csv, index=False)
    print(f"combined {len(files)} files -> {output_csv} "
          f"({len(df):,} rows, {df['registered_voters'].sum():,} voters)")
    return output_csv