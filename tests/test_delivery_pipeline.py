import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.batch_pipeline import connect, create_batch, set_repository  # noqa: E402
from tools.delivery_records import EXPORT_HEADERS  # noqa: E402


COLLECTOR = ROOT / ".agents/skills/cc-usr-delivery-producer/scripts/collect_record.py"
LOCATOR = ROOT / ".agents/skills/cc-usr-delivery-producer/scripts/find_claude_turns.py"
QC = ROOT / ".agents/skills/cc-usr-delivery-qc/scripts/validate_records.py"
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


def spec() -> dict:
    return {
        "batch": "0911",
        "brief": "交付测试",
        "questions": [{
            "folder": "q001",
            "task_id": "0911-001",
            "title": "跨模块事件协作服务",
            "prompt": "从零构建一套事件协作服务，覆盖事件接入、顺序处理、持久化、状态推送、断线重连和完整验证场景。",
            "task_type": "0-1 代码生成",
            "difficulty": "困难",
            "languages": ["Go", "TypeScript"],
            "repo_url": "https://github.com/example/project",
            "initial_snapshot": "",
            "reproducibility": "无外部依赖",
            "expected_areas": ["service", "web", "tests"],
            "difficulty_evidence": ["跨模块事件顺序"],
            "similarity_tags": ["state-ordering"],
        }],
    }


def automated_record() -> dict:
    values = {
        "session_id": "session-001",
        "turn_id": "turn-001",
        "other_issues": "",
        "submitter": "测试提交人",
        "turn_completed_at": "2026-09-07T10:00:00+08:00",
        "submitted_at": "2026-09-07T11:00:00+08:00",
        "human_authored": False,
    }
    descriptions = {
        "delivery": "接口、持久化和状态推送均已实现，集成测试覆盖断线重连后继续接收事件，完整测试集通过。",
        "instruction": "逐项核对 prompt 后，指定技术栈、目录边界和禁止项都与提交内容一致，没有发现越界改动。",
        "planning": "实现顺序先固定数据流和状态约束，再完成服务端与页面联调，最后收敛到测试验证，阶段衔接清楚。",
        "reasoning": "顺序处理和重连恢复共享同一游标语义，代码中的边界判断与测试场景一致，关键设计没有自相矛盾。",
        "execution": "文件检索、修改和验证都围绕目标模块展开，失败命令得到及时纠正，最终构建与测试均正常结束。",
    }
    for prefix in ("delivery", "instruction", "planning", "reasoning", "execution"):
        values[f"{prefix}_score"] = 5
        values[f"{prefix}_description"] = descriptions[prefix]
    return values


class DeliveryPipelineTests(unittest.TestCase):
    def prepare(self, root: Path) -> Path:
        database = root / "production.sqlite3"
        spec_path = root / "spec.json"
        spec_path.write_text(json.dumps(spec(), ensure_ascii=False), encoding="utf-8")
        connection = connect(database)
        create_batch(connection, root, spec_path)
        question = connection.execute("SELECT * FROM questions").fetchone()
        set_repository(
            connection,
            "0911",
            1,
            "https://github.com/example/project",
            f"https://github.com/example/project/commit/{question['local_initial_sha']}",
        )
        question = connection.execute("SELECT * FROM questions").fetchone()
        connection.execute(
            "UPDATE questions SET status='running' WHERE id=?", (question["id"],)
        )
        connection.execute(
            "INSERT INTO runs(question_id, batch_run_id, launched_at, harness, harness_version) "
            "VALUES(?, 'run-001', '2026-09-07T09:00:00+08:00', "
            "'Claude Code', '2.1.259 (Claude Code)')",
            (question["id"],),
        )
        connection.commit()
        connection.close()
        return database

    def run_command(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False
        )

    def test_automated_record_qc_approval_and_excel_export(self):
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
            self.assertEqual(run["session_id"], "session-001")

            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            result = self.run_command([
                sys.executable, str(QC), "--db", str(database), "--batch", "0911",
                "--approve", "0911-001-T01", "--reviewer", "human-qc",
                "--confirm-human-review", "YES",
            ])
            self.assertEqual(result.returncode, 0, result.stdout)

            output = root / "CC_Codex 用户满意度标注（0911）.xlsx"
            result = self.run_command([
                sys.executable, str(EXPORTER), "--db", str(database), "--batch", "0911",
                "--template", str(TEMPLATE), "--output", str(output),
            ])
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue(output.is_file())
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                root_xml = ET.fromstring(archive.read(data_sheet_path(archive)))
            rows = root_xml.findall(f"{{{NS}}}sheetData/{{{NS}}}row")
            self.assertEqual(len(rows), 2)
            headers = []
            for cell in rows[0].findall(f"{{{NS}}}c"):
                node = cell.find(f"{{{NS}}}is/{{{NS}}}t")
                headers.append("" if node is None else node.text)
            self.assertEqual(headers, EXPORT_HEADERS)
            harness_cell = rows[1].find(f"{{{NS}}}c[@r='F2']/{{{NS}}}is/{{{NS}}}t")
            self.assertIsNotNone(harness_cell)
            self.assertEqual(harness_cell.text, "Claude Code")
            score_cell = rows[1].find(f"{{{NS}}}c[@r='L2']")
            self.assertIsNotNone(score_cell)
            self.assertEqual(score_cell.attrib.get("t"), "n")

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
            self.assertIn("evaluator self-reference", result.stdout)
            self.assertIn("prohibited fixed label", result.stdout)

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
            self.assertEqual(
                [turn["prompt_id"] for turn in located["turns"]],
                ["prompt-001", "prompt-002"],
            )


if __name__ == "__main__":
    unittest.main()
