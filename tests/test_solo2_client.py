import unittest

from tools.solo2_client import Solo2Error, map_record_to_schema


class Solo2MappingTests(unittest.TestCase):
    def test_maps_remote_builtin_keys_and_attachment_without_column_order(self):
        schema = {
            "fingerprint": "schema-v1",
            "fields": [
                {"field_key": "user_prompt", "label": "User Prompt", "field_type": "textarea", "is_required": True},
                {"field_key": "env_snapshot", "label": "初始环境快照", "field_type": "url", "is_required": True},
                {"field_key": "score_delivery", "label": "交付完整性", "field_type": "number", "is_required": True},
                {"field_key": "desc_delivery", "label": "交付完整性 - 描述", "field_type": "textarea", "is_required": True},
                {"field_key": "trajectory_file", "label": "轨迹文件", "field_type": "attachment", "is_required": True},
                {"field_key": "question_type", "label": "任务类型", "field_type": "select", "is_required": True, "max_length": 0, "options": ["0-1代码生成"]},
                {"field_key": "x_iteration", "label": "当前对话轮次排序", "field_type": "number", "is_required": True, "max_length": 0},
            ],
        }
        record = {
            "user_prompt": "实现一个完整服务",
            "initial_snapshot": "https://github.com/example/repo/commit/" + "a" * 40,
            "delivery_score": 4,
            "delivery_description": "主要功能已经实现，接口行为也有对应验证。",
            "trajectory_file": "session.jsonl",
            "task_type": "0-1 代码生成",
            "turn_no": 1,
        }

        data, attachment_keys = map_record_to_schema(record, schema)

        self.assertEqual(data["env_snapshot"], record["initial_snapshot"])
        self.assertEqual(data["score_delivery"], 4)
        self.assertEqual(data["desc_delivery"], record["delivery_description"])
        self.assertEqual(data["trajectory_file"], [])
        self.assertEqual(data["question_type"], "0-1代码生成")
        self.assertEqual(data["x_iteration"], 1)
        self.assertEqual(attachment_keys, ["trajectory_file"])

    def test_rejects_unmapped_required_remote_field(self):
        schema = {
            "fields": [{
                "field_key": "x_required_by_server",
                "label": "平台新增必填字段",
                "field_type": "text",
                "is_required": True,
            }],
        }

        with self.assertRaisesRegex(Solo2Error, "平台新增必填字段"):
            map_record_to_schema({}, schema)


if __name__ == "__main__":
    unittest.main()
