#!/usr/bin/env python3
"""Export QC-passed delivery records and their original trajectory files."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.batch_pipeline import (  # noqa: E402
    batch_row,
    connect,
    parse_selection,
    question_rows,
)
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
        sheets = workbook.findall(f"{{{NS_MAIN}}}sheets/{{{NS_MAIN}}}sheet")
        if len(sheets) != 1 or sheets[0].attrib.get("name") != "数据表":
            raise ValueError("template must contain only the 数据表 worksheet")
        sheet = sheets[0]
        relationship_id = sheet.attrib[f"{{{NS_REL}}}id"]
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationship = rels.find(f"{{{NS_PKG_REL}}}Relationship[@Id='{relationship_id}']")
        if relationship is None:
            raise ValueError("template worksheet relationship is missing")
        target = relationship.attrib["Target"].lstrip("/")
        return target if target.startswith("xl/") else f"xl/{target}"


def shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.iter(f"{{{NS_MAIN}}}t"))
        for item in root.findall(f"{{{NS_MAIN}}}si")
    ]


def cell_text(cell: ET.Element, strings: list[str]) -> str:
    if cell.attrib.get("t") == "s":
        node = cell.find(f"{{{NS_MAIN}}}v")
        if node is None or node.text is None:
            return ""
        index = int(node.text)
        return strings[index] if 0 <= index < len(strings) else ""
    if cell.attrib.get("t") == "str":
        node = cell.find(f"{{{NS_MAIN}}}v")
        return "" if node is None or node.text is None else node.text
    nodes = cell.findall(f"{{{NS_MAIN}}}is//{{{NS_MAIN}}}t")
    return "".join(node.text or "" for node in nodes)


def make_cell(reference: str, value: object, numeric: bool) -> ET.Element:
    attributes = {"r": reference}
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
        strings = shared_strings(archive)
    sheet_data = root.find(f"{{{NS_MAIN}}}sheetData")
    if sheet_data is None:
        raise ValueError("template worksheet has no sheetData")
    rows = sheet_data.findall(f"{{{NS_MAIN}}}row")
    if not rows:
        raise ValueError("template worksheet has no header row")
    header_cells = rows[0].findall(f"{{{NS_MAIN}}}c")
    actual_headers = [cell_text(cell, strings) for cell in header_cells]
    if actual_headers != HEADERS:
        raise ValueError("template headers do not match the required 28-column contract")
    for row in rows[1:]:
        sheet_data.remove(row)
    for row_index, record in enumerate(records, start=2):
        row = ET.SubElement(sheet_data, f"{{{NS_MAIN}}}row", {"r": str(row_index)})
        for column_index, key in enumerate(KEYS, start=1):
            # The uploaded trajectory URL is filled manually after submission.
            value = "" if key == "trajectory_file" else record[key]
            if key == "languages" and isinstance(value, list):
                value = ", ".join(str(item) for item in value)
            reference = f"{column_name(column_index)}{row_index}"
            row.append(
                make_cell(reference, value, key in SCORE_KEYS or key == "turn_no")
            )
    dimension = root.find(f"{{{NS_MAIN}}}dimension")
    if dimension is not None:
        dimension.set("ref", f"A1:{column_name(len(KEYS))}{len(records) + 1}")
    ET.register_namespace("", NS_MAIN)
    ET.register_namespace("r", NS_REL)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def export(template: Path, output: Path, records: list[dict]) -> None:
    if template.resolve() == output.resolve():
        raise ValueError("output must not overwrite the template")
    if output.exists():
        raise FileExistsError(f"output exists: {output}")
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


def question_label(numbers: list[int]) -> str:
    if not numbers:
        raise ValueError("cannot build a file name without question numbers")
    ranges: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return "第" + "_".join(ranges) + "题"


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for suffix in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}_{suffix}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError(f"too many existing exports for {path.name}")


def trajectory_sources(claude_root: Path, records: list[dict]) -> list[tuple[int, Path]]:
    requested: dict[tuple[int, str, str], None] = {}
    for record in records:
        requested[(
            int(record["question_no"]),
            str(record["session_id"]),
            str(record["trajectory_file"]),
        )] = None
    by_name: dict[str, list[Path]] = {}
    for path in claude_root.rglob("*.jsonl"):
        if path.is_file():
            by_name.setdefault(path.name, []).append(path)
    located: list[tuple[int, Path]] = []
    for question_no, session_id, filename in requested:
        candidates = sorted(by_name.get(filename, []))
        if not candidates:
            raise FileNotFoundError(
                f"trajectory not found for question {question_no}: {filename}"
            )
        exact = [path for path in candidates if path.stem == session_id]
        if exact:
            candidates = exact
        if len(candidates) != 1:
            raise ValueError(
                f"trajectory is ambiguous for question {question_no}: {filename}"
            )
        located.append((question_no, candidates[0]))
    return located


def copy_trajectories(
    sources: list[tuple[int, Path]], batch: str, batch_directory: Path
) -> list[Path]:
    copied: list[Path] = []
    for question_no, source in sources:
        destination = unique_path(
            batch_directory / f"轨迹_{batch}_第{question_no}题_{source.name}"
        )
        shutil.copy2(source, destination)
        copied.append(destination)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("production.sqlite3"))
    parser.add_argument("--batch", required=True)
    parser.add_argument("--select", help="question numbers, for example 1,3-5")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--claude-root", type=Path, default=Path.home() / ".claude/projects")
    parser.add_argument(
        "--template", type=Path,
        default=Path(__file__).resolve().parent.parent / "assets" / "CC_Codex 用户满意度标注（试标）.xlsx",
    )
    args = parser.parse_args()
    try:
        template = args.template.resolve()
        if not template.is_file():
            raise FileNotFoundError(f"template not found: {template}")
        connection = connect(args.db.resolve())
        batch = batch_row(connection, args.batch)
        records = load_database_records(connection, args.batch)
        if args.select:
            selected_questions = parse_selection(
                args.select, question_rows(connection, args.batch)
            )
            selected_numbers = {
                int(question["question_no"]) for question in selected_questions
            }
            records = [
                record for record in records
                if int(record["question_no"]) in selected_numbers
            ]
            missing = selected_numbers - {
                int(record["question_no"]) for record in records
            }
            if missing:
                missing_text = ", ".join(str(number) for number in sorted(missing))
                raise ValueError(f"selected questions have no delivery records: {missing_text}")
        connection.close()
        if not records:
            raise ValueError(f"batch has no delivery records: {args.batch}")
        errors, warnings = validate_records(records, require_delivery_qc=True)
        if errors:
            raise ValueError("QC-passed records failed export validation: " + "; ".join(errors))
        if warnings:
            raise ValueError("QC-passed records have unresolved warnings: " + "; ".join(warnings))
        numbers = sorted({int(record["question_no"]) for record in records})
        batch_directory = Path(batch["folder_path"]).resolve()
        if not batch_directory.is_dir():
            raise FileNotFoundError(f"batch directory not found: {batch_directory}")
        if args.output:
            output = args.output.resolve()
            if output.parent != batch_directory:
                raise ValueError("output must be placed directly in the batch directory")
        else:
            output = unique_path(
                batch_directory
                / f"CC_Codex 用户满意度标注（{args.batch}-{question_label(numbers)}）.xlsx"
            )
        sources = trajectory_sources(args.claude_root.resolve(), records)
        export(template, output, records)
        trajectories = copy_trajectories(sources, args.batch, batch_directory)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    print(f"Exported {len(records)} records to {output}")
    for trajectory in trajectories:
        print(f"Exported trajectory: {trajectory}")
    print("Duplicate record IDs: 0; duplicate SessionID + TurnID pairs: 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
