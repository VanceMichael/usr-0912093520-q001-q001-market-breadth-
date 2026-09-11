import json
import hashlib
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.batch_pipeline import connect, create_batch, set_repository  # noqa: E402
from tools.delivery_records import EXPORT_HEADERS  # noqa: E402
from tools.solo2_client import Solo2Error  # noqa: E402
from tools.solo2_service import submit_records  # noqa: E402
from tools.human_review import (  # noqa: E402
    approve_codex_record,
    approve_record,
    build_codex_delivery_regeneration_dossier,
    build_codex_review_dossier,
    reject_record,
    review_queue,
)
from tools import orchestrator  # noqa: E402
from webapp.server import ConsoleData  # noqa: E402


COLLECTOR = ROOT / ".agents/skills/cc-usr-delivery-producer/scripts/collect_record.py"
LOCATOR = ROOT / ".agents/skills/cc-usr-delivery-producer/scripts/find_claude_turns.py"
QC = ROOT / ".agents/skills/cc-usr-delivery-qc/scripts/validate_records.py"
STYLE_GATE = ROOT / ".agents/skills/cc-usr-delivery-producer/scripts/check_description_style.py"
EXPORTER = ROOT / ".agents/skills/cc-usr-excel-exporter/scripts/export_xlsx.py"
TEMPLATE = ROOT / ".agents/skills/cc-usr-excel-exporter/assets/CC_Codex 用户满意度标注（试标）.xlsx"
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def data_sheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    sheet = workbook.find(f"{{{NS}}}sheets/{{{NS}}}sheet[@name='数据表']")
    relationship_id = sheet.attrib[f"{{{NS_REL}}}id"]
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relationship = relationships.find(
        f"{{{NS_PKG_REL}}}Relationship[@Id='{relationship_id}']"
    )
    target = relationship.attrib["Target"].lstrip("/")
    return target if target.startswith("xl/") else f"xl/{target}"


def direct_cell_text(cell: ET.Element) -> str:
    if cell.attrib.get("t") == "str":
        node = cell.find(f"{{{NS}}}v")
        return "" if node is None or node.text is None else node.text
    nodes = cell.findall(f"{{{NS}}}is//{{{NS}}}t")
    return "".join(node.text or "" for node in nodes)


def spec(count: int = 1) -> dict:
    return {
        "batch": "0911",
        "brief": "交付测试",
        "questions": [{
            "folder": f"q{number:03d}",
            "task_id": f"0911-{number:03d}",
            "title": f"跨模块事件协作服务 {number}",
            "prompt": "从零构建一套事件协作服务，覆盖事件接入、顺序处理、持久化、状态推送、断线重连和完整验证场景。",
            "task_type": "0-1 代码生成",
            "difficulty": "困难",
            "languages": ["Go", "TypeScript"],
            "repo_url": f"https://github.com/example/project-{number}",
            "initial_snapshot": "",
            "reproducibility": "无外部依赖",
            "expected_areas": ["service", "web", "tests"],
            "difficulty_evidence": ["跨模块事件顺序"],
            "similarity_tags": [f"state-ordering-{number}"],
        } for number in range(1, count + 1)],
    }


def attach_evidence(record: dict, number: int = 1, line: int = 4, result_text: str = "实现与验证均已完成。") -> dict:
    descriptions = {
        prefix: record[f"{prefix}_description"]
        for prefix in ("delivery", "instruction", "planning", "reasoning", "execution")
    }
    final_event = {
        "type": "assistant", "sessionId": f"session-{number:03d}",
        "message": {"role": "assistant", "content": [{"type": "text", "text": result_text}]},
    }
    final_line = json.dumps(final_event, ensure_ascii=False)
    line_hash = hashlib.sha256(final_line.encode("utf-8")).hexdigest()
    evidence = []
    for prefix, description in descriptions.items():
        sentences = [part.strip() for part in re.split(r"[。！？]", description) if part.strip()]
        for index, sentence in enumerate(sentences, 1):
            evidence.append({
                "id": f"{prefix}-{index}", "dimension": prefix,
                "source_type": "trajectory", "line": line,
                "source_sha256": line_hash, "excerpt": result_text.rstrip("。"),
                "claim": sentence, "fact": "目标轮次留下了完成和验证结果",
            })
    record["evidence_ledger"] = evidence
    record["requirement_coverage"] = [{
        "requirement": "覆盖事件接入、顺序处理、持久化、状态推送、断线重连和完整验证场景",
        "status": "met", "evidence_ids": ["delivery-1"],
    }]
    return record


def automated_record(number: int = 1) -> dict:
    values = {
        "session_id": f"session-{number:03d}",
        "turn_id": f"turn-{number:03d}",
        "trajectory_file": f"session-{number:03d}.jsonl",
        "other_issues": "",
        "submitter": "测试提交人",
        "turn_completed_at": "2026-09-07T10:00:00+08:00",
        "submitted_at": "2026-09-07T11:00:00+08:00",
        "human_authored": False,
    }
    descriptions = {
        "delivery": "接口、持久化和状态推送都已经落到可运行代码中，集成测试覆盖断线重连后继续接收事件。主要业务链路能够完整闭环，现有验证没有留下影响交付的缺口。",
        "instruction": "题目指定的技术栈、目录边界和禁止事项均与提交内容一致，相关实现没有越过允许范围。各项显式要求都能在代码或测试中找到对应结果，交付行为与原始约束一致。",
        "planning": "实现过程先固定数据流与状态约束，再完成服务端和页面之间的联调，最后集中执行测试验证。阶段之间有明确的前后依赖，遇到失败时也能回到对应环节继续处理。",
        "reasoning": "顺序处理与重连恢复共用同一套游标语义，边界判断和测试场景能够相互印证。关键取舍保持前后一致，异常分支没有引入与正常流程冲突的状态解释。",
        "execution": "文件检索、代码修改和验证命令都集中在目标模块，出现失败后能够根据错误位置及时修正。最终构建与完整测试均正常结束，没有重复执行无关操作拖慢交付。",
    }
    if number % 2 == 0:
        descriptions = {
            "delivery": "断线恢复、事件落库和订阅推送已经形成连续处理链路，验收过程覆盖了连接恢复后的增量消息。运行结果没有暴露会阻断主要业务路径的缺失项。",
            "instruction": "实现范围保持在题目约定的后端服务和验证代码内，指定的持久化及顺序约束均有对应落点。原始要求中的限制没有被额外功能或越界修改破坏。",
            "planning": "工作先梳理事件进入后的状态变化，再分别推进存储、订阅和恢复路径，收尾阶段统一核对异常场景。每个阶段都有对应进展反馈，前后步骤能够互相衔接。",
            "reasoning": "重连后的游标延续被作为状态一致性的关键条件，并用恢复场景验证这一判断。正常传输和连接中断采用同一顺序语义，没有出现互相矛盾的处理分支。",
            "execution": "检索范围围绕事件处理目录展开，修改完成后依次运行构建和验收脚本。命令遇到问题时先定位对应位置再修正，结束前的验证正常返回且没有无关重复调用。",
        }
    for prefix in ("delivery", "instruction", "planning", "reasoning", "execution"):
        values[f"{prefix}_score"] = 5
        values[f"{prefix}_description"] = descriptions[prefix]
    return attach_evidence(values, number)


def approve_all(database: Path) -> None:
    connection = connect(database)
    record_ids = [str(row[0]) for row in connection.execute("SELECT record_id FROM records ORDER BY id")]
    connection.close()
    for record_id in record_ids:
        approve_record(
            database, record_id, "gaoyong",
            {dimension: True for dimension in ("delivery", "instruction", "planning", "reasoning", "execution")},
        )


def codex_review(*, approved: bool = True) -> dict:
    return {
        "decision": "approved" if approved else "rejected",
        "summary": "五个维度的描述、分数和来源证据能够逐项对应。" if approved else "任务规划的描述缺少当前轮次直接证据。",
        "dimensions": {
            dimension: {
                "approved": approved,
                "reason": "描述中的判断能够由当前轮次的原始轨迹内容直接核对。" if approved else "当前资料没有提供足够的原始轨迹内容支持该项判断。",
            }
            for dimension in ("delivery", "instruction", "planning", "reasoning", "execution")
        },
        "issues": [] if approved else ["任务规划缺少直接证据"],
    }


def bind_evidence_to_line(record: dict, trajectory: Path, line: int, excerpt: str) -> dict:
    raw = trajectory.read_bytes().splitlines()[line - 1]
    for item in record["evidence_ledger"]:
        item.update({
            "line": line,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "excerpt": excerpt,
            "fact": "目标轮次中的原始事件可以直接核对",
        })
    return record


def valid_trajectory(question: object, number: int = 1) -> str:
    session_id = f"session-{number:03d}"
    turn_id = f"turn-{number:03d}"
    tool_id = f"tool-{number:03d}"
    events = [
        {
            "type": "user", "sessionId": session_id, "promptId": turn_id,
            "cwd": question["folder_path"],
            "message": {"role": "user", "content": question["prompt"]},
        },
        {
            "type": "assistant", "sessionId": session_id,
            "message": {"role": "assistant", "content": [{
                "type": "tool_use", "id": tool_id, "name": "Read",
                "input": {"file_path": str(Path(question["folder_path"]) / "README.md")},
            }]},
        },
        {
            "type": "user", "sessionId": session_id, "promptId": turn_id,
            "message": {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": tool_id, "content": "ok",
            }]},
        },
        {
            "type": "assistant", "sessionId": session_id,
            "message": {"role": "assistant", "content": [{
                "type": "text", "text": "实现与验证均已完成。",
            }]},
        },
    ]
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n"


class DeliveryPipelineTests(unittest.TestCase):
    def prepare(self, root: Path, count: int = 1) -> Path:
        database = root / "production.sqlite3"
        spec_path = root / "spec.json"
        spec_path.write_text(json.dumps(spec(count), ensure_ascii=False), encoding="utf-8")
        connection = connect(database)
        create_batch(connection, root, spec_path)
        questions = connection.execute(
            "SELECT * FROM questions ORDER BY question_no"
        ).fetchall()
        for question in questions:
            repository = f"https://github.com/example/project-{question['question_no']}"
            set_repository(
                connection,
                "0911",
                question["question_no"],
                repository,
                f"{repository}/commit/{question['local_initial_sha']}",
            )
        connection.execute(
            "UPDATE questions SET status='running'"
        )
        for question in questions:
            connection.execute(
                "INSERT INTO runs(question_id, batch_run_id, launched_at, harness, harness_version,status,trajectory_root) "
                "VALUES(?, ?, '2026-09-07T09:00:00+08:00', "
                "'Claude Code', '2.1.259','succeeded',?)",
                (question["id"], f"run-{question['question_no']:03d}", str(root / "0911")),
            )
            (root / "0911" / f"session-{question['question_no']:03d}.jsonl").write_text(
                valid_trajectory(question, question["question_no"]), encoding="utf-8",
            )
        connection.commit()
        connection.close()
        return database

    def run_command(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False
        )

    def test_description_style_gate_rejects_multiline_or_overlong_description(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record = automated_record()
            record["delivery_description"] += "\n补充说明"
            input_path = root / "score.json"
            input_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(STYLE_GATE), "--input", str(input_path),
            ])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("delivery_description must be a single paragraph", result.stdout)

            record["delivery_description"] = "接口测试确认服务返回一致结果。" * 30
            input_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(STYLE_GATE), "--input", str(input_path),
            ])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("delivery_description must not exceed 420 characters", result.stdout)

    def test_delivery_descriptions_reject_stock_and_model_wording(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            record = automated_record()
            record["human_authored"] = True
            record["planning_description"] = "本次任务中，总体而言，模型的表现符合预期，规划过程比较完整。"
            input_path = root / "score.json"
            input_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("套话开头", result.stdout)
            connection = connect(database)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0], 0)
            connection.close()

    def test_delivery_qc_finalize_and_excel_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            connection = connect(database)
            stored = connection.execute("SELECT * FROM records").fetchone()
            run = connection.execute("SELECT * FROM runs").fetchone()
            connection.close()
            self.assertEqual(stored["human_authored"], 0)
            expected_os = "Windows" if sys.platform == "win32" else "MacOS/Linux"
            self.assertEqual(stored["operating_system"], expected_os)
            self.assertEqual(run["session_id"], "session-001")

            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
                "--finalize",
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("质检通过", result.stdout)
            connection = connect(database)
            qc_record = connection.execute("SELECT * FROM records").fetchone()
            connection.close()
            self.assertEqual(qc_record["delivery_qc_passed"], 1)
            self.assertEqual(qc_record["delivery_qc_note"], "质检通过")
            self.assertTrue(qc_record["delivery_qc_checked_at"])
            approve_all(database)

            claude_root = root / "claude-projects"
            claude_root.mkdir()
            (claude_root / "session-001.jsonl").write_text(
                '{"sessionId":"session-001"}\n', encoding="utf-8"
            )
            result = self.run_command([
                sys.executable, str(EXPORTER), "--db", str(database), "--batch", "0911",
                "--template", str(TEMPLATE), "--claude-root", str(claude_root),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            output = root / "0911/CC_Codex 用户满意度标注（0911-第1题）.xlsx"
            self.assertTrue(output.is_file())
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                workbook = ET.fromstring(archive.read("xl/workbook.xml"))
                sheets = workbook.findall(f"{{{NS}}}sheets/{{{NS}}}sheet")
                self.assertEqual([sheet.attrib["name"] for sheet in sheets], ["数据表"])
                root_xml = ET.fromstring(archive.read(data_sheet_path(archive)))
            rows = root_xml.findall(f"{{{NS}}}sheetData/{{{NS}}}row")
            self.assertEqual(len(rows), 2)
            headers = [
                direct_cell_text(cell)
                for cell in rows[0].findall(f"{{{NS}}}c")
            ]
            self.assertEqual(headers, EXPORT_HEADERS)
            turn_order_cell = rows[1].find(f"{{{NS}}}c[@r='D2']")
            self.assertIsNotNone(turn_order_cell)
            self.assertEqual(turn_order_cell.attrib.get("t"), "n")
            self.assertEqual(turn_order_cell.find(f"{{{NS}}}v").text, "1")
            trajectory_cell = rows[1].find(f"{{{NS}}}c[@r='F2']/{{{NS}}}is/{{{NS}}}t")
            self.assertIsNotNone(trajectory_cell)
            self.assertIsNone(trajectory_cell.text)
            harness_cell = rows[1].find(f"{{{NS}}}c[@r='H2']/{{{NS}}}is/{{{NS}}}t")
            self.assertIsNotNone(harness_cell)
            self.assertEqual(harness_cell.text, "Claude Code")
            note_cell = rows[1].find(f"{{{NS}}}c[@r='AB2']/{{{NS}}}is/{{{NS}}}t")
            self.assertIsNotNone(note_cell)
            self.assertEqual(note_cell.text, "质检通过")
            score_cell = rows[1].find(f"{{{NS}}}c[@r='N2']")
            self.assertIsNotNone(score_cell)
            self.assertEqual(score_cell.attrib.get("t"), "n")
            trajectories = list((root / "0911").glob("轨迹_0911_第1题_session-001*.jsonl"))
            self.assertEqual(len(trajectories), 1)
            self.assertEqual(
                trajectories[0].read_text(encoding="utf-8"),
                '{"sessionId":"session-001"}\n',
            )

            result = self.run_command([
                sys.executable, str(EXPORTER), "--db", str(database), "--batch", "0911",
                "--template", str(TEMPLATE), "--claude-root", str(claude_root),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(
                (root / "0911/CC_Codex 用户满意度标注（0911-第1题）_2.xlsx").is_file()
            )
            self.assertEqual(
                len(list((root / "0911").glob("轨迹_0911_第1题_session-001*.jsonl"))),
                2,
            )

    def test_codex_five_dimension_review_can_release_final_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8")
            self.assertEqual(self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ]).returncode, 0)
            self.assertEqual(self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911", "--finalize",
            ]).returncode, 0)
            dossier = build_codex_review_dossier(database, "0911-001-T01")
            self.assertEqual(set(dossier["scores"]), {
                "delivery", "instruction", "planning", "reasoning", "execution",
            })

            result = approve_codex_record(database, "0911-001-T01", codex_review())

            self.assertEqual(result["review_method"], "codex")
            with connect(database) as connection:
                record = connection.execute(
                    "SELECT human_qc_approved,human_qc_reviewer,review_method FROM records"
                ).fetchone()
                dimensions = connection.execute(
                    "SELECT dimension,reviewer FROM record_dimension_reviews ORDER BY dimension"
                ).fetchall()
            self.assertEqual(record["human_qc_approved"], 1)
            self.assertEqual(record["review_method"], "codex")
            self.assertIn("Codex", record["human_qc_reviewer"])
            self.assertEqual(len(dimensions), 5)
            self.assertTrue(all("Codex" in row["reviewer"] for row in dimensions))
            with self.assertRaisesRegex(ValueError, "已经完成最终复核"):
                approve_codex_record(database, "0911-001-T01", codex_review())
            with self.assertRaisesRegex(ValueError, "已经完成最终复核"):
                approve_record(
                    database,
                    "0911-001-T01",
                    "gaoyong",
                    {
                        dimension: True
                        for dimension in ("delivery", "instruction", "planning", "reasoning", "execution")
                    },
                )

    def test_codex_review_rejects_partial_or_blocked_verdict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8")
            self.assertEqual(self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ]).returncode, 0)
            with connect(database) as connection:
                connection.execute(
                    "UPDATE records SET delivery_qc_passed=1,delivery_qc_note='质检通过',"
                    "delivery_qc_checked_at='2026-09-07T12:00:00+08:00'"
                )
                connection.commit()
            partial = codex_review()
            partial["dimensions"].pop("planning")
            with self.assertRaisesRegex(ValueError, "五维结论|未通过维度"):
                approve_codex_record(database, "0911-001-T01", partial)
            blocked = codex_review()
            blocked["issues"] = ["仍有一项事实不能确认"]
            with self.assertRaisesRegex(ValueError, "阻断问题"):
                approve_codex_record(database, "0911-001-T01", blocked)
            with connect(database) as connection:
                approved = connection.execute(
                    "SELECT human_qc_approved FROM records"
                ).fetchone()[0]
            self.assertEqual(approved, 0)

    def test_codex_delivery_regeneration_reuses_record_and_resets_review_gates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(
                json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8"
            )
            self.assertEqual(self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ]).returncode, 0)
            self.assertEqual(self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911", "--finalize",
            ]).returncode, 0)

            dossier = build_codex_delivery_regeneration_dossier(
                database, "0911-001-T01"
            )
            self.assertEqual(Path(dossier["trajectory_root"]), root / "0911")
            self.assertEqual(Path(dossier["question_folder"]), root / "0911" / "q001")
            payload = {
                "dimensions": {
                    name: {
                        "score": dossier["current"][f"{name}_score"],
                        "description": dossier["current"][f"{name}_description"],
                    }
                    for name in ("delivery", "instruction", "planning", "reasoning", "execution")
                },
                "other_issues": dossier["current"]["other_issues"],
                "evidence_ledger": dossier["evidence_ledger"],
                "requirement_coverage": dossier["requirement_coverage"],
            }
            data = ConsoleData(database, root)
            try:
                result = data._apply_regenerated_delivery(
                    "0911-001-T01", dossier, payload,
                    ("delivery", "instruction", "planning", "reasoning", "execution"),
                )
            finally:
                data.shutdown()

            self.assertEqual(result["record_id"], "0911-001-T01")
            self.assertEqual(
                result["before"]["other_issues"], dossier["current"]["other_issues"]
            )
            self.assertEqual(
                result["before"]["evidence_ledger"], dossier["evidence_ledger"]
            )
            self.assertEqual(
                result["before"]["requirement_coverage"], dossier["requirement_coverage"]
            )
            self.assertIn("delivery_qc_note", result["previous_audit"])
            self.assertIn("dimension_reviews", result["previous_audit"])
            with connect(database) as connection:
                count = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                record = connection.execute(
                    "SELECT delivery_qc_passed,evidence_gate_passed,history_gate_passed,"
                    "human_qc_approved,review_method FROM records WHERE record_id=?",
                    ("0911-001-T01",),
                ).fetchone()
            self.assertEqual(count, 1)
            self.assertEqual(tuple(record), (0, 0, 0, 0, ""))

    def test_codex_delivery_regeneration_rejects_modified_evidence_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(
                json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8"
            )
            self.assertEqual(self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ]).returncode, 0)

            dossier = build_codex_delivery_regeneration_dossier(
                database, "0911-001-T01"
            )
            payload = {
                "dimensions": {
                    name: {
                        "score": dossier["current"][f"{name}_score"],
                        "description": dossier["current"][f"{name}_description"],
                    }
                    for name in ("delivery", "instruction", "planning", "reasoning", "execution")
                },
                "other_issues": dossier["current"]["other_issues"],
                "evidence_ledger": [dict(item) for item in dossier["evidence_ledger"]],
                "requirement_coverage": dossier["requirement_coverage"],
            }
            payload["evidence_ledger"][0]["excerpt"] = "Codex 改写后的虚假证据"
            with connect(database) as connection:
                before = connection.execute(
                    "SELECT delivery_description,evidence_ledger FROM records WHERE record_id=?",
                    ("0911-001-T01",),
                ).fetchone()

            data = ConsoleData(database, root)
            try:
                with self.assertRaisesRegex(ValueError, "权威来源被修改"):
                    data._apply_regenerated_delivery(
                        "0911-001-T01", dossier, payload,
                        ("delivery", "instruction", "planning", "reasoning", "execution"),
                    )
            finally:
                data.shutdown()

            with connect(database) as connection:
                after = connection.execute(
                    "SELECT delivery_description,evidence_ledger FROM records WHERE record_id=?",
                    ("0911-001-T01",),
                ).fetchone()
            self.assertEqual(tuple(after), tuple(before))

    def test_trajectory_evidence_cannot_use_a_later_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            with connect(database) as connection:
                question = connection.execute("SELECT * FROM questions").fetchone()
            trajectory = root / "0911/session-001.jsonl"
            later_user = {
                "type": "user", "sessionId": "session-001", "promptId": "turn-002",
                "message": {"role": "user", "content": "继续完善异常处理"},
            }
            later_assistant = {
                "type": "assistant", "sessionId": "session-001",
                "message": {"role": "assistant", "content": "后续验证已经完成。"},
            }
            with trajectory.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(later_user, ensure_ascii=False) + "\n")
                stream.write(json.dumps(later_assistant, ensure_ascii=False) + "\n")
            record = automated_record()
            raw = trajectory.read_bytes().splitlines()[5]
            for item in record["evidence_ledger"]:
                item.update({
                    "line": 6,
                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                    "excerpt": "后续验证已经完成",
                })
            input_path = root / "score.json"
            input_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("不属于当前轮次范围", result.stdout)

    def test_later_failed_retry_does_not_hide_successful_session_trajectory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            connection = connect(database)
            question_id = connection.execute("SELECT id FROM questions").fetchone()[0]
            connection.execute(
                "INSERT INTO runs(question_id,batch_run_id,launched_at,status,session_id,trajectory_root) "
                "VALUES(?,'later-failed','2026-09-07T12:00:00+08:00','failed','failed-session',?)",
                (question_id, str(root / "missing-failed-trajectory")),
            )
            connection.commit()
            connection.close()

            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
            ])
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_excel_export_selects_questions_and_all_their_turns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root, count=2)
            for question_no in (1, 2):
                input_path = root / f"score-{question_no}.json"
                input_path.write_text(
                    json.dumps(automated_record(question_no), ensure_ascii=False),
                    encoding="utf-8",
                )
                result = self.run_command([
                    sys.executable, str(COLLECTOR), "--db", str(database),
                    "--batch", "0911", "--question", str(question_no),
                    "--turn", "1", "--from-json", str(input_path),
                ])
                self.assertEqual(result.returncode, 0, result.stdout)
            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
                "--finalize",
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            approve_all(database)
            claude_root = root / "claude-projects"
            claude_root.mkdir()
            for question_no in (1, 2):
                (claude_root / f"session-{question_no:03d}.jsonl").write_text(
                    json.dumps({"sessionId": f"session-{question_no:03d}"}) + "\n",
                    encoding="utf-8",
                )
            result = self.run_command([
                sys.executable, str(EXPORTER), "--db", str(database), "--batch", "0911",
                "--select", "2", "--template", str(TEMPLATE),
                "--claude-root", str(claude_root),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            output = root / "0911/CC_Codex 用户满意度标注（0911-第2题）.xlsx"
            self.assertTrue(output.is_file())
            with zipfile.ZipFile(output) as archive:
                root_xml = ET.fromstring(archive.read(data_sheet_path(archive)))
            rows = root_xml.findall(f"{{{NS}}}sheetData/{{{NS}}}row")
            self.assertEqual(len(rows), 2)
            session_cell = rows[1].find(f"{{{NS}}}c[@r='B2']/{{{NS}}}is/{{{NS}}}t")
            self.assertEqual(session_cell.text, "session-002")
            self.assertFalse(list((root / "0911").glob("轨迹_0911_第1题_*.jsonl")))
            self.assertEqual(
                len(list((root / "0911").glob("轨迹_0911_第2题_*.jsonl"))), 1
            )

    def test_delivery_qc_applies_audited_fix_before_finalize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(
                json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8"
            )
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            connection = connect(database)
            connection.execute("UPDATE records SET submitter='Codex'")
            connection.commit()
            connection.close()

            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
            ])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("real person's name", result.stdout)

            fixes = {
                "records": [{
                    "record_id": "0911-001-T01",
                    "reason": "提交人误写为工具名称，按已确认的真实提交人修正",
                    "changes": {"submitter": "测试提交人"},
                }]
            }
            fixes_path = root / "fixes.json"
            fixes_path.write_text(json.dumps(fixes, ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
                "--fixes", str(fixes_path),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            connection = connect(database)
            fixed = connection.execute("SELECT * FROM records").fetchone()
            connection.close()
            self.assertEqual(fixed["submitter"], "测试提交人")
            self.assertEqual(fixed["delivery_qc_passed"], 0)
            changes = json.loads(fixed["delivery_qc_changes"])
            self.assertEqual(changes[0]["changes"]["submitter"]["before"], "Codex")
            self.assertEqual(changes[0]["changes"]["submitter"]["after"], "测试提交人")

            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
                "--finalize",
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("质检通过", result.stdout)

    def test_excel_export_rejects_records_without_delivery_qc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(
                json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8"
            )
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            output = root / "unpassed.xlsx"
            result = self.run_command([
                sys.executable, str(EXPORTER), "--db", str(database), "--batch", "0911",
                "--template", str(TEMPLATE), "--output", str(output),
            ])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("delivery QC is not passed", result.stdout)
            self.assertFalse(output.exists())

    def test_delivery_qc_rejects_launched_question_without_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root, count=2)
            input_path = root / "score.json"
            input_path.write_text(
                json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8"
            )
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
                "--finalize",
            ])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("0911-002: launched question has no delivery record", result.stdout)

    def test_delivery_qc_rejects_absolute_container_skill_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(
                json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8"
            )
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            connection = connect(database)
            question = connection.execute("SELECT * FROM questions").fetchone()
            trajectory_root = root / "worker-state" / "claude"
            trajectory_root.mkdir(parents=True)
            connection.execute(
                "UPDATE runs SET batch_run_id='docker-test',trajectory_root=?,container_cwd='/workspace'",
                (str(trajectory_root),),
            )
            connection.commit()
            connection.close()
            events = [json.loads(line) for line in valid_trajectory(question).splitlines()]
            events[1]["message"]["content"][0]["input"] = {
                "file_path": "/workspace/../project/.agents/skills/other/SKILL.md",
            }
            (trajectory_root / "session-001.jsonl").write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8",
            )

            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
            ])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("external skill path", result.stdout)

    def test_delivery_qc_rejects_nonconsecutive_dialogue_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(
                json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8"
            )
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            connection = connect(database)
            connection.execute("UPDATE records SET turn_no=2")
            connection.commit()
            connection.close()

            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
            ])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("turn_no must be consecutive from 1", result.stdout)

    def test_automated_record_rejects_meta_and_template_language(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            values = automated_record()
            values["delivery_description"] = "AI 分析认为功能已经完成。"
            values["planning_description"] = "When: 修改配置；What: 没有验证；Impact: 交付状态不确定。"
            input_path = root / "score.json"
            input_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("评价者自述", result.stdout)
            self.assertIn("固定标签", result.stdout)

    def test_automated_record_rejects_repeated_descriptions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            values = automated_record()
            repeated = "指定模块已实现并通过测试，代码与用户要求一致，没有发现影响交付的问题。"
            for prefix in ("delivery", "instruction", "planning", "reasoning", "execution"):
                values[f"{prefix}_description"] = repeated
            input_path = root / "score.json"
            input_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("must not repeat verbatim", result.stdout)

    def test_other_issues_rejects_evaluator_english_and_repeated_dimension(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            values = automated_record()
            values["other_issues"] = "Overall，错误提示没有指出具体失败字段，排查请求时仍需读取服务端日志。"
            input_path = root / "score.json"
            input_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("英文评价", result.stdout)

            values["other_issues"] = values["delivery_description"]
            input_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("outside the five score dimensions", result.stdout)

    def test_description_allows_summary_technical_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            values = automated_record()
            values["other_issues"] = "服务保留了 /summary 路由和 SummaryService 类，JSON 字段 `summary` 的命名与现有协议一致。"
            input_path = root / "score.json"
            input_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)

    def test_automated_record_rejects_tool_as_submitter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            values = automated_record()
            values["submitter"] = "Codex"
            input_path = root / "score.json"
            input_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ])
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("real person's name", result.stdout)

    def test_locator_extracts_session_and_each_prompt_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            connection = connect(database)
            question = connection.execute("SELECT * FROM questions").fetchone()
            connection.close()
            claude_root = root / "claude-projects/project"
            claude_root.mkdir(parents=True)
            events = [
                {
                    "type": "user",
                    "cwd": question["folder_path"],
                    "sessionId": "session-claude-001",
                    "uuid": "prompt-001",
                    "timestamp": "2026-09-07T09:00:05+08:00",
                    "message": {"role": "user", "content": question["prompt"]},
                },
                {
                    "type": "assistant",
                    "sessionId": "session-claude-001",
                    "timestamp": "2026-09-07T09:01:00+08:00",
                    "message": {"role": "assistant", "content": "done"},
                },
                {
                    "type": "user",
                    "cwd": question["folder_path"],
                    "sessionId": "session-claude-001",
                    "promptId": "prompt-002",
                    "timestamp": "2026-09-07T09:02:00+08:00",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "继续"}],
                    },
                },
            ]
            trajectory = claude_root / "session-claude-001.jsonl"
            trajectory.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )
            result = self.run_command([
                sys.executable, str(LOCATOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--claude-root", str(root / "claude-projects"),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            located = json.loads(result.stdout)
            self.assertEqual(located["session_id"], "session-claude-001")
            self.assertEqual(located["trajectory_file"], "session-claude-001.jsonl")
            self.assertEqual(
                [turn["prompt_id"] for turn in located["turns"]],
                ["prompt-001", "prompt-001"],
            )
            self.assertEqual(located["turns"][1]["raw_turn_id"], "prompt-002")
            self.assertEqual(located["turns"][1]["raw_user_prompt"], "继续")
            self.assertTrue(located["turns"][1]["is_continuation"])

    def test_locator_uses_registered_docker_trajectory_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            connection = connect(database)
            question = connection.execute("SELECT * FROM questions").fetchone()
            trajectory_root = root / "worker-state" / "claude"
            project_root = trajectory_root / "projects" / "-workspace"
            project_root.mkdir(parents=True)
            connection.execute(
                "UPDATE runs SET batch_run_id='docker-test',trajectory_root=?",
                (str(trajectory_root),),
            )
            connection.commit()
            connection.close()
            event = {
                "type": "user",
                "cwd": "/workspace",
                "sessionId": "docker-session",
                "uuid": "docker-prompt",
                "timestamp": "2026-09-07T09:00:05+08:00",
                "message": {"role": "user", "content": question["prompt"]},
            }
            (project_root / "docker-session.jsonl").write_text(
                json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8",
            )
            result = self.run_command([
                sys.executable, str(LOCATOR), "--db", str(database),
                "--batch", "0911", "--question", "1",
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            located = json.loads(result.stdout)
            self.assertEqual(located["session_id"], "docker-session")
            self.assertEqual(located["trajectory_file"], "docker-session.jsonl")

    def test_interrupted_continue_turn_reuses_delivery_identity_and_keeps_raw_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            connection = connect(database)
            question = connection.execute("SELECT * FROM questions").fetchone()
            connection.close()
            events = [
                {
                    "type": "user", "sessionId": "session-001", "promptId": "turn-001",
                    "cwd": question["folder_path"],
                    "message": {"role": "user", "content": question["prompt"]},
                },
                {
                    "type": "assistant", "sessionId": "session-001",
                    "message": {"role": "assistant", "content": [{
                        "type": "tool_use", "id": "tool-continue", "name": "Read",
                        "input": {"file_path": str(Path(question["folder_path"]) / "README.md")},
                    }]},
                },
                {
                    "type": "user", "sessionId": "session-001", "promptId": "turn-001",
                    "message": {"role": "user", "content": [{
                        "type": "tool_result", "tool_use_id": "tool-continue", "content": "ok",
                    }]},
                },
                {
                    "type": "user", "sessionId": "session-001", "promptId": "turn-continue",
                    "cwd": question["folder_path"],
                    "message": {"role": "user", "content": "继续"},
                },
                {
                    "type": "assistant", "sessionId": "session-001",
                    "message": {"role": "assistant", "content": [{
                        "type": "text", "text": "中断后已经恢复工作并完成实现。",
                    }]},
                },
            ]
            trajectory_path = root / "0911/session-001.jsonl"
            trajectory_path.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )
            first = bind_evidence_to_line(automated_record(), trajectory_path, 2, "tool-continue")
            first_path = root / "first.json"
            first_path.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
            first_result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(first_path),
            ])
            self.assertEqual(first_result.returncode, 0, first_result.stdout)

            continued = automated_record(2)
            continued.update({
                "record_id": "0911-001-T02",
                "session_id": "session-001",
                "trajectory_file": "session-001.jsonl",
                "turn_id": "turn-001",
                "user_prompt": question["prompt"],
                "raw_user_prompt": "继续",
                "raw_turn_id": "turn-continue",
                "is_continuation": True,
                "continuation_count": 1,
                "task_type": question["task_type"],
                "difficulty": question["difficulty"],
                "languages": question["languages"],
                "other_issues": "执行过程因响应限制发生中断，输入一次继续后恢复处理并完成剩余工作。",
            })
            bind_evidence_to_line(continued, trajectory_path, 5, "中断后已经恢复工作并完成实现")
            continued_path = root / "continued.json"
            continued_path.write_text(
                json.dumps(continued, ensure_ascii=False), encoding="utf-8",
            )
            second_result = self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "2", "--from-json", str(continued_path),
            ])
            self.assertEqual(second_result.returncode, 0, second_result.stdout)
            qc_result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
            ])
            self.assertEqual(qc_result.returncode, 0, qc_result.stdout)
            connection = connect(database)
            rows = connection.execute(
                "SELECT turn_id,raw_turn_id,is_continuation FROM records ORDER BY turn_no"
            ).fetchall()
            connection.close()
            self.assertEqual([row["turn_id"] for row in rows], ["turn-001", "turn-001"])
            self.assertEqual(rows[1]["raw_turn_id"], "turn-continue")
            self.assertEqual(rows[1]["is_continuation"], 1)

    def test_solo2_submission_is_idempotent_and_uploads_original_trajectory(self):
        class Client:
            uploads: list[Path] = []
            submissions: list[dict] = []

            def form_schema(self):
                return {"fingerprint": "schema-1", "fields": [
                    {"field_key": "user_prompt", "field_type": "text", "is_required": True},
                    {"field_key": "trace_file", "field_type": "file", "is_required": True},
                ]}

            def upload(self, path):
                self.uploads.append(Path(path))
                return {"name": Path(path).name, "path": "/uploads/trajectory.jsonl", "size": 10}

            def create_submission(self, data, fingerprint):
                self.submissions.append({"data": data, "fingerprint": fingerprint})
                return {"id": "remote-1", "status": "SUBMITTED"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8")
            self.assertEqual(self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ]).returncode, 0)
            self.assertEqual(self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911", "--finalize",
            ]).returncode, 0)
            approve_all(database)

            client = Client()
            factory = lambda *_args: client
            first = submit_records(
                database, root / "cookies", "https://solo2.example.com",
                batch="0911", client_factory=factory,
            )
            second = submit_records(
                database, root / "cookies", "https://solo2.example.com",
                batch="0911", client_factory=factory,
            )
            self.assertEqual((first["submitted"], first["failed"]), (1, 0))
            self.assertEqual((second["submitted"], second["skipped"]), (0, 1))
            self.assertEqual(Client.uploads[0].name, "session-001.jsonl")
            self.assertEqual(Client.submissions[0]["fingerprint"], "schema-1")
            self.assertEqual(len(Client.submissions), 1)

    def test_solo2_can_submit_one_exact_delivery_record(self):
        class Client:
            submissions: list[str] = []

            def form_schema(self):
                return {"fingerprint": "schema-1", "fields": [
                    {"field_key": "user_prompt", "field_type": "text", "is_required": True},
                ]}

            def create_submission(self, data, _fingerprint):
                self.submissions.append(data["user_prompt"])
                return {"id": "remote-exact", "status": "SUBMITTED"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root, count=2)
            for number in (1, 2):
                input_path = root / f"score-{number}.json"
                input_path.write_text(
                    json.dumps(automated_record(number), ensure_ascii=False), encoding="utf-8",
                )
                result = self.run_command([
                    sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                    "--question", str(number), "--turn", "1", "--from-json", str(input_path),
                ])
                self.assertEqual(result.returncode, 0, result.stdout)
            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911", "--finalize",
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            approve_all(database)

            before = review_queue(database, "0911")["records"]
            self.assertTrue(all(item["can_solo2_submit"] for item in before))
            result = submit_records(
                database, root / "cookies", "https://solo2.example.com",
                batch="0911", record_ids={"0911-002-T01"},
                client_factory=lambda *_args: Client(),
            )

            self.assertEqual((result["submitted"], result["failed"]), (1, 0))
            self.assertEqual(Client.submissions, [before[1]["user_prompt"]])
            after = {item["record_id"]: item for item in review_queue(database, "0911")["records"]}
            self.assertEqual(after["0911-002-T01"]["solo2_status"], "succeeded")
            self.assertEqual(after["0911-002-T01"]["solo2_remote_id"], "remote-exact")
            self.assertFalse(after["0911-002-T01"]["can_solo2_submit"])
            self.assertTrue(after["0911-001-T01"]["can_solo2_submit"])
            self.assertEqual(review_queue(database, "0911")["summary"]["delivered"], 1)
            with connect(database) as connection:
                connection.execute(
                    "UPDATE records SET human_qc_approved=0,human_qc_reviewer='',"
                    "human_qc_approved_at='',review_method='' WHERE record_id='0911-002-T01'"
                )
                connection.commit()
            console = ConsoleData(database, root)
            try:
                question = console.dashboard("0911")["questions"][1]
                self.assertTrue(question["delivered"])
                self.assertEqual(question["stage_label"], "已交付")
            finally:
                console.shutdown()
            with self.assertRaisesRegex(ValueError, "已经提交到 SOLO2"):
                reject_record(database, "0911-002-T01", "gaoyong", "需要重新修改评分描述")

    def test_solo2_504_waits_then_manual_retry_recovers(self):
        class Client:
            should_fail = True

            def form_schema(self):
                return {"fingerprint": "schema-1", "fields": [
                    {"field_key": "user_prompt", "field_type": "text", "is_required": True},
                ]}

            def create_submission(self, _data, _fingerprint):
                if self.should_fail:
                    raise Solo2Error("网关超时", status=504)
                return {"id": "remote-after-retry", "status": "SUBMITTED"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8")
            self.assertEqual(self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ]).returncode, 0)
            self.assertEqual(self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911", "--finalize",
            ]).returncode, 0)
            approve_all(database)
            client = Client()
            factory = lambda *_args: client
            failed = submit_records(
                database, root / "cookies", "https://solo2.example.com",
                batch="0911", manual=False, client_factory=factory,
            )
            self.assertEqual(failed["results"][0]["status"], "retry_wait")
            self.assertFalse(orchestrator.solo2_pending_available(database, 3))
            with connect(database) as connection:
                connection.execute(
                    "UPDATE solo2_submissions SET updated_at='2026-01-01T00:00:00+08:00'"
                )
                connection.commit()
            self.assertTrue(orchestrator.solo2_pending_available(database, 3))
            client.should_fail = False
            retried = submit_records(
                database, root / "cookies", "https://solo2.example.com",
                batch="0911", manual=True, client_factory=factory,
            )
            self.assertEqual(retried["submitted"], 1)

    def test_solo2_worker_skips_a_concurrently_claimed_record(self):
        class Client:
            submissions: list[str] = []

            def form_schema(self):
                return {"fingerprint": "schema-1", "fields": [
                    {"field_key": "user_prompt", "field_type": "text", "is_required": True},
                ]}

            def create_submission(self, data, _fingerprint):
                self.submissions.append(data["user_prompt"])
                return {"id": "remote-2", "status": "SUBMITTED"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root, count=2)
            for number in (1, 2):
                input_path = root / f"score-{number}.json"
                input_path.write_text(
                    json.dumps(automated_record(number), ensure_ascii=False), encoding="utf-8",
                )
                self.assertEqual(self.run_command([
                    sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                    "--question", str(number), "--turn", "1", "--from-json", str(input_path),
                ]).returncode, 0)
            self.assertEqual(self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911", "--finalize",
            ]).returncode, 0)
            approve_all(database)
            with connect(database) as connection:
                first = connection.execute(
                    "SELECT record_id,question_id FROM records ORDER BY question_id LIMIT 1"
                ).fetchone()
                connection.execute(
                    "INSERT INTO solo2_submissions(record_id,question_id,status,attempt_count,"
                    "updated_at,lease_owner,lease_expires_at) VALUES(?,?,'submitting',1,"
                    "'2026-09-10T10:00:00+08:00','other-worker','2099-01-01T00:00:00+08:00')",
                    (first["record_id"], first["question_id"]),
                )
                connection.commit()

            client = Client()
            result = submit_records(
                database, root / "cookies", "https://solo2.example.com", limit=1,
                manual=False, client_factory=lambda *_args: client,
            )
            self.assertEqual((result["submitted"], result["failed"]), (1, 0))
            with connect(database) as connection:
                statuses = connection.execute(
                    "SELECT status FROM solo2_submissions ORDER BY question_id"
                ).fetchall()
            self.assertEqual([row["status"] for row in statuses], ["submitting", "succeeded"])

    def test_solo2_schema_auth_failure_is_recorded(self):
        class Client:
            def form_schema(self):
                raise Solo2Error("登录已失效", status=401)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = self.prepare(root)
            input_path = root / "score.json"
            input_path.write_text(json.dumps(automated_record(), ensure_ascii=False), encoding="utf-8")
            self.assertEqual(self.run_command([
                sys.executable, str(COLLECTOR), "--db", str(database), "--batch", "0911",
                "--question", "1", "--turn", "1", "--from-json", str(input_path),
            ]).returncode, 0)
            self.assertEqual(self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911", "--finalize",
            ]).returncode, 0)
            approve_all(database)

            result = submit_records(
                database, root / "cookies", "https://solo2.example.com", manual=False,
                client_factory=lambda *_args: Client(),
            )

            self.assertEqual(result["results"][0]["status"], "auth_blocked")
            with connect(database) as connection:
                row = connection.execute(
                    "SELECT status,last_error,lease_owner FROM solo2_submissions"
                ).fetchone()
            self.assertEqual((row["status"], row["last_error"]), ("auth_blocked", "登录已失效"))
            self.assertEqual(row["lease_owner"], "")


if __name__ == "__main__":
    unittest.main()
