from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def write_docx(path: Path, text: str) -> None:
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{escaped}</w:t></w:r></w:p></w:body></w:document>"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)


def write_pdf(path: Path, title: str) -> None:
    escaped = title.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 16 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref = len(document)
    document.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    document.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(document)


def create_dataset(root: Path, force: bool = False) -> None:
    if root.exists() and any(root.iterdir()):
        if not force:
            raise SystemExit(f"{root} is not empty. Re-run with --force to replace this demo sandbox.")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    (root / "invoice_aug.txt").write_text("Invoice August 2026. Vendor: Example Cloud. Total: 125 USD.", encoding="utf-8")
    write_pdf(root / "invoice_sep.pdf", "Invoice September 2026")
    (root / "project_alpha_architecture.md").write_text(
        "# Project Alpha architecture\nAPI gateway, worker queue, and PostgreSQL design.", encoding="utf-8"
    )
    write_docx(root / "meeting_notes.docx", "Project Alpha meeting notes: review architecture and roadmap.")
    (root / "project_alpha_report.md").write_text("# Project Alpha report\nCurrent final project status.", encoding="utf-8")
    (root / "project_alpha_report_v2.md").write_text("# Project Alpha report\nOlder version 2 project status.", encoding="utf-8")
    (root / "sales_data.csv").write_text("month,revenue\nAugust,1000\nSeptember,1200\n", encoding="utf-8")
    (root / "sales_data_new.csv").write_text("month,revenue\nAugust,1100\nSeptember,1300\n", encoding="utf-8")
    write_pdf(root / "project_alpha_final.pdf", "Project Alpha final presentation")
    shutil.copyfile(root / "project_alpha_final.pdf", root / "project_alpha_final (1).pdf")
    (root / "notes.txt").write_text("Remember to follow up next week.", encoding="utf-8")
    with zipfile.ZipFile(root / "archive_backup.zip", "w") as archive:
        archive.writestr("opaque.bin", b"unsupported demo content")

    old_timestamp = 1_704_067_200  # 2024-01-01 UTC
    os.utime(root / "project_alpha_report_v2.md", (old_timestamp, old_timestamp))
    print(f"Created {len(list(root.iterdir()))} synthetic files in {root}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a synthetic messy sandbox.")
    parser.add_argument("target", nargs="?", type=Path, default=PROJECT_ROOT / "sandbox")
    parser.add_argument("--force", action="store_true", help="Replace an existing demo sandbox")
    args = parser.parse_args()
    create_dataset(args.target.resolve(), args.force)


if __name__ == "__main__":
    main()
