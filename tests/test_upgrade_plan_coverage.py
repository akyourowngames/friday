"""Coverage checks for the spreadsheet-driven upgrade plan."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile


NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def _col_index(cell_ref: str) -> int:
    letters = re.match(r"[A-Z]+", cell_ref).group(0)
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _workbook_rows(path: Path) -> list[dict[str, str]]:
    with ZipFile(path) as workbook:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
            for item in root.findall("main:si", NS):
                shared.append("".join(t.text or "" for t in item.findall(".//main:t", NS)))

        wb = ET.fromstring(workbook.read("xl/workbook.xml"))
        rels = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels.findall("pkgrel:Relationship", NS)
        }
        sheet_target = ""
        for sheet in wb.findall("main:sheets/main:sheet", NS):
            if sheet.attrib["name"] == "Upgrade Plan":
                rid = sheet.attrib[f"{{{NS['rel']}}}id"]
                sheet_target = rid_to_target[rid]
                break
        assert sheet_target
        if not sheet_target.startswith("xl/"):
            sheet_target = "xl/" + sheet_target

        sheet = ET.fromstring(workbook.read(sheet_target))
        rows: list[list[str]] = []
        for row in sheet.findall("main:sheetData/main:row", NS):
            values: list[str] = []
            for cell in row.findall("main:c", NS):
                index = _col_index(cell.attrib["r"])
                while len(values) <= index:
                    values.append("")
                value = cell.find("main:v", NS)
                inline = cell.find("main:is", NS)
                if cell.attrib.get("t") == "s" and value is not None:
                    values[index] = shared[int(value.text)]
                elif cell.attrib.get("t") == "inlineStr" and inline is not None:
                    values[index] = "".join(t.text or "" for t in inline.findall(".//main:t", NS))
                elif value is not None:
                    values[index] = value.text or ""
            if any(item.strip() for item in values):
                rows.append(values)

    headers = rows[0]
    return [
        {headers[index]: row[index] if index < len(row) else "" for index in range(len(headers))}
        for row in rows[1:]
    ]


def test_workbook_rows_are_represented_in_upgrade_implementation_doc():
    root = Path(__file__).resolve().parents[1]
    rows = _workbook_rows(root / "docs" / "existing-tools-skills-upgrade-plan.xlsx")
    implementation = (root / "docs" / "existing-tools-skills-upgrade-implementation.md").read_text(
        encoding="utf-8"
    )

    missing = [
        row["Existing Tool / Skill"]
        for row in rows
        if row["Existing Tool / Skill"] not in implementation
    ]

    assert not missing
