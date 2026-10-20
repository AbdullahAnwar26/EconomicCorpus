import fitz
from pathlib import Path

pdf_path = Path("reports/Economics_Books_Free_PDF_Links.pdf")
output_path = Path("reports/economic_sources_pdf.txt")

document = fitz.open(pdf_path)

text = "\n".join(
    page.get_text("text")
    for page in document
)

document.close()

output_path.write_text(
    text,
    encoding="utf-8"
)

print(f"Pages: {len(fitz.open(pdf_path))}")
print(f"Characters: {len(text):,}")
print(f"Saved: {output_path}")
