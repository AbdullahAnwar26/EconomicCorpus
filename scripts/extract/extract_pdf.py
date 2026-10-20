import fitz


def extract_pdf(path):
    document = fitz.open(path)

    pages = []

    for page_number, page in enumerate(document, start=1):
        text = page.get_text("text")

        pages.append({
            "page": page_number,
            "text": text
        })

    document.close()

    return pages
