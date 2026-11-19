from pypdf import PdfReader

from rental_agent import pdf


def test_document_prints_every_block_with_links_and_page_numbers(tmp_path):
    out = tmp_path / "folder" / "doc.pdf"
    url = "https://example.com/a--b__c?x=1"
    pdf.write(out, title="Evidence – 2027", subtitle="12 Oak St", footer="Foot", blocks=[
        "A paragraph with **bold** and Ω.",
        pdf.Heading("Section"),
        pdf.Facts([("Label", "Value"),
                   ("A label that is much too long for one line of the label column", "Other")]),
        pdf.Table(("Name", "Amount"), [["row", "$1"]] * 80, widths=(70, 30),
                  right=frozenset({1}), bold=frozenset({0})),
        pdf.Links([url]),
    ])
    reader = PdfReader(out)
    pages = [page.extract_text() for page in reader.pages]
    text = "\n".join(pages)
    assert len(pages) > 1 and reader.metadata.title == "Evidence – 2027"
    assert "A paragraph with bold and ?." in text         # Helvetica has no Greek letters
    assert "Label Value" in text and text.count("row") == 80
    assert all("Name" in page and f"Page {n} of {len(pages)}" in page
               for n, page in enumerate(pages, start=1))  # headings repeat on every page
    assert f"1.  {url}" in text                           # printed in full, markup untouched
    links = [a.get_object()["/A"]["/URI"] for page in reader.pages
             for a in page.get("/Annots", [])]
    assert links == [url]
