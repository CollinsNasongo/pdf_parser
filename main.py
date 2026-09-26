"""
Split the IEBC register-of-voters PDFs into pages, parse each page to CSV,
then combine the page CSVs into one CSV per year.

Usage (from the project root):
    python main.py                  # both years
    python main.py 2017             # one year
    python main.py 2022 --resplit   # force re-splitting the source PDF
    python main.py --combine-only   # just rebuild the combined CSVs
"""
import argparse
import sys

from paths.config import CSV_DIR, RAW_PDF_DIR, SPLIT_PDF_DIR
from parser.process_pdf import (
    LAYOUTS,
    combine_csvs,
    split_pdf_to_folder,
    transform_folder_to_csv,
)

YEARS = ("2017", "2022")


def run(year: str, resplit: bool = False, combine_only: bool = False) -> None:
    raw_pdf = RAW_PDF_DIR / year / f"rov_per_polling_station_{year}.pdf"
    csv_folder = CSV_DIR / raw_pdf.stem              # where transform writes pages

    if not combine_only:
        if not raw_pdf.is_file():
            raise FileNotFoundError(f"missing source PDF: {raw_pdf}")

        # Splitting 700-1,200 pages is slow; reuse existing pages unless asked not to.
        pages_dir = SPLIT_PDF_DIR / raw_pdf.stem
        if resplit or not any(pages_dir.glob("page_*.pdf")):
            print(f"[{year}] splitting {raw_pdf.name} ...")
            pages_dir = split_pdf_to_folder(raw_pdf)
        else:
            print(f"[{year}] reusing split pages in {pages_dir}")

        csv_folder = transform_folder_to_csv(pages_dir, CSV_DIR, layout=year)

    combine_csvs(csv_folder)   # -> CSV_DIR / rov_per_polling_station_<year>.csv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("years", nargs="*", metavar="YEAR",
                    help=f"years to process, any of {', '.join(YEARS)} (default: all)")
    ap.add_argument("--resplit", action="store_true",
                    help="re-split source PDFs even if pages already exist")
    ap.add_argument("--combine-only", action="store_true",
                    help="skip split/parse; only rebuild the combined CSVs")
    args = ap.parse_args(argv)

    years = args.years or list(YEARS)
    for year in years:
        if year not in YEARS or year not in LAYOUTS:
            ap.error(f"unsupported year {year!r}; choose from {', '.join(YEARS)}")

    for year in years:
        run(year, resplit=args.resplit, combine_only=args.combine_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())