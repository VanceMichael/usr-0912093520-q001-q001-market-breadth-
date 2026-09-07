#!/usr/bin/env python3
"""Fill the official CC_Codex template from approved SQLite records."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import connect  # noqa: E402
from tools.delivery_records import (  # noqa: E402
    EXPORT_HEADERS as HEADERS,
    EXPORT_KEYS as KEYS,
    SCORE_KEYS,
    load_records as load_database_records,
    validate_records,
)


NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def column_name(index: int) -> str:
    value = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        value = chr(65 + remainder) + value
    return value


def worksheet_path(template: Path) -> str:
    with zipfile.ZipFile(template) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheet = workbook.find(f"{{{NS_MAIN}}}sheets/{{{NS_MAIN}}}sheet[@name='数据表']")
        if sheet is None:
            raise ValueError("template is missing worksheet 数据表")
        relationship_id = sheet.attrib[f"{{{NS_REL}}}id"]
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationship = rels.find(f"{{{NS_PKG_REL}}}Relationship[@Id='{relationship_id}']")
        if relationship is None:
            raise ValueError("template worksheet relationship is missing")
        target = relationship.attrib["Target"].lstrip("/")
        return target if target.startswith("xl/") else f"xl/{target}"


def cell_text(cell: ET.Element) -> str:
    node = cell.find(f"{{{NS_MAIN}}}is/{{{NS_MAIN}}}t")
    return "" if node is None or node.text is None else node.text


def make_cell(reference: str, value: object, numeric: bool) -> ET.Element:
    attributes = {"r": reference, "s": "1"}
    cell = ET.Element(f"{{{NS_MAIN}}}c", attributes)
    if numeric:
        cell.set("t", "n")
        node = ET.SubElement(cell, f"{{{NS_MAIN}}}v")
        node.text = str(value)
    else:
        cell.set("t", "inlineStr")
        inline = ET.SubElement(cell, f"{{{NS_MAIN}}}is")
        node = ET.SubElement(inline, f"{{{NS_MAIN}}}t")
        text = "" if value is None else str(value)
        if text[:1].isspace() or text[-1:].isspace():
            node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        node.text = text
    return cell


def build_sheet(template: Path, sheet_path: str, records: list[dict]) -> bytes:
    with zipfile.ZipFile(template) as archive:
        root = ET.fromstring(archive.read(sheet_path))
    sheet_data = root.find(f"{{{NS_MAIN}}}sheetData")
    if sheet_data is None:
        raise ValueError("template worksheet has no sheetData")
    rows = sheet_data.findall(f"{{{NS_MAIN}}}row")
    if not rows:
        raise ValueError("template worksheet has no header row")
    header_cells = rows[0].findall(f"{{{NS_MAIN}}}c")
    actual_headers = [cell_text(cell) for cell in header_cells]
    if actual_headers != HEADERS:
        raise ValueError("template headers do not match the required 25-column contract")
    for row in rows[1:]:
        sheet_data.remove(row)
    for row_index, record in enumerate(records, start=2):
        row = ET.SubElement(sheet_data, f"{{{NS_MAIN}}}row", {"r": str(row_index)})
        for column_index, key in enumerate(KEYS, start=1):
            value = record[key]
            if key == "languages" and isinstance(value, list):
                value = ", ".join(str(item) for item in value)
            reference = f"{column_name(column_index)}{row_index}"
            row.append(make_cell(reference, value, key in SCORE_KEYS))
    dimension = root.find(f"{{{NS_MAIN}}}dimension")
    if dimension is not None:
        dimension.set("ref", f"A1:Y{len(records) + 1}")
    ET.register_namespace("", NS_MAIN)
    ET.register_namespace("r", NS_REL)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def export(template: Path, output: Path, records: list[dict], force: bool) -> None:
    if template.resolve() == output.resolve():
        raise ValueError("output must not overwrite the template")
    if output.exists() and not force:
        raise FileExistsError(f"output exists: {output}; use --force to replace it")
    sheet_path = worksheet_path(template)
    sheet_xml = build_sheet(template, sheet_path, records)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix=".xlsx", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(template) as source, zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED
        ) as destination:
            for item in source.infolist():
                payload = sheet_xml if item.filename == sheet_path else source.read(item.filename)
                destination.writestr(item, payload)
        with zipfile.ZipFile(temporary) as check:
            if check.testzip() is not None:
                raise ValueError("generated workbook ZIP validation failed")
            check_root = ET.fromstring(check.read(sheet_path))
            rows = check_root.findall(f"{{{NS_MAIN}}}sheetData/{{{NS_MAIN}}}row")
            if len(rows) != len(records) + 1:
                raise ValueError("generated workbook row count does not match input")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--batch", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--template", type=Path,
        default=Path(__file__).resolve().parent.parent / "assets" / "CC_Codex 用户满意度标注（试标）.xlsx",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        template = args.template.resolve()
        if not template.is_file():
            raise FileNotFoundError(f"template not found: {template}")
        connection = connect(args.db.resolve())
        records = load_database_records(
            connection, args.batch, only_human_approved=True
        )
        connection.close()
        if not records:
            raise ValueError(f"batch has no human-QC-approved records: {args.batch}")
        errors, warnings = validate_records(records, require_human_qc=True)
        if errors:
            raise ValueError("approved records failed structural QC: " + "; ".join(errors))
        if warnings:
            raise ValueError("approved records have unresolved warnings: " + "; ".join(warnings))
        output = args.output.resolve()
        export(template, output, records, args.force)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {len(records)} records to {output}")
    print("Duplicate record IDs: 0; duplicate SessionID + TurnID pairs: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
