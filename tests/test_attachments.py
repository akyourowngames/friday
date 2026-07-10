import base64
import io
import zipfile

from PIL import Image

from ares.attachments import build_attachment_context, inspect_attachment


def _data(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def test_inspect_text_upload_and_guard_context():
    inspection = inspect_attachment({
        "name": "notes.md",
        "type": "text/markdown",
        "data": _data(b"# Notes\nHello Ares"),
    })

    assert inspection.kind == "text"
    assert "Hello Ares" in inspection.content
    context = build_attachment_context([inspection])
    assert "untrusted file contents" in context
    assert "### notes.md" in context


def test_inspect_image_includes_dimensions_and_vision_payload():
    output = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(output, format="PNG")

    inspection = inspect_attachment({
        "name": "screen.png",
        "type": "image/png",
        "data": _data(output.getvalue()),
    })

    assert inspection.kind == "image"
    assert "12 × 8" in inspection.content
    assert inspection.vision_data_url.startswith("data:image/png;base64,")


def test_inspect_docx_extracts_xml_text():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="urn:test"><w:body><w:p><w:t>Hello document</w:t></w:p></w:body></w:document>',
        )

    inspection = inspect_attachment({
        "name": "brief.docx",
        "data": _data(output.getvalue()),
    })

    assert inspection.kind == "office document text"
    assert "Hello document" in inspection.content


def test_inspect_pdf_extracts_text():
    raw = b"%PDF-1.4\nBT (Hello attached PDF) Tj ET\n%%EOF"

    inspection = inspect_attachment({
        "name": "report.pdf", "type": "application/pdf", "data": _data(raw)
    })

    assert inspection.kind == "PDF text"
    assert "Hello attached PDF" in inspection.content


def test_inspect_archive_lists_entries():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("folder/readme.txt", "hello")

    inspection = inspect_attachment({"name": "bundle.zip", "data": _data(output.getvalue())})

    assert inspection.kind == "archive listing"
    assert "folder/readme.txt" in inspection.content


def test_inspect_local_path(tmp_path):
    target = tmp_path / "data.json"
    target.write_text('{"ok": true}', encoding="utf-8")

    inspection = inspect_attachment({"name": target.name, "path": str(target)})

    assert inspection.path == str(target.resolve())
    assert '"ok": true' in inspection.content
