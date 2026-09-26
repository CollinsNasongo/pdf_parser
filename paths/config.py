from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
PDF_DIR = PROJECT_DIR / "pdf"
CSV_DIR = PDF_DIR / "csv_pages"
RAW_PDF_DIR = PDF_DIR / "raw_pdf"
SPLIT_PDF_DIR = PDF_DIR / "pdf_pages"
SPLIT_CSV_DIR = PDF_DIR / "csv_pages"
LOGS = SPLIT_PDF_DIR / "logs.txt"