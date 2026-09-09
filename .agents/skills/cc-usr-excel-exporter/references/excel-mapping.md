# Excel column mapping

The `数据表` worksheet has exactly 28 exported columns:

| Column | Header | SQLite column |
| --- | --- | --- |
| A | User Prompt | user_prompt |
| B | SessionID | session_id |
| C | TurnID/PromptID | turn_id |
| D | 当前对话轮次排序 | turn_no |
| E | 初始环境快照 | initial_snapshot |
| F | 轨迹文件 | 留空；上传轨迹后由用户填写 |
| G | 环境可复现等级 | reproducibility |
| H | Harness | harness |
| I | Harness 版本 | harness_version |
| J | 操作系统 | operating_system |
| K | 任务类型 | task_type |
| L | 任务难度 | difficulty |
| M | 语言/框架 | languages |
| N | 交付完整性 | delivery_score |
| O | 交付完整性 - 描述 | delivery_description |
| P | 指令遵循 | instruction_score |
| Q | 指令遵循 - 描述 | instruction_description |
| R | 任务规划 | planning_score |
| S | 任务规划 - 描述 | planning_description |
| T | 推理能力 | reasoning_score |
| U | 推理能力 - 描述 | reasoning_description |
| V | 执行能力 | execution_score |
| W | 执行能力 - 描述 | execution_description |
| X | 其他问题 | other_issues |
| Y | 提交人 | submitter |
| Z | 提交时间 | submitted_at |
| AA | 父记录 | parent_record |
| AB | 审核备注 | delivery_qc_note |

`当前对话轮次排序` and scores remain numeric. Identifiers, timestamps, prompts, descriptions, and parent IDs remain text.

`trajectory_file` remains required in SQLite for session validation and locating the original JSONL, but it is intentionally not written to column F. The exporter copies that JSONL unchanged into the batch root with the batch and question number in its file name.
