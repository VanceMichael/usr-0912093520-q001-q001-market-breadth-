# Excel column mapping

The `数据表` worksheet has exactly 27 exported columns:

| Column | Header | SQLite column |
| --- | --- | --- |
| A | User Prompt | user_prompt |
| B | SessionID | session_id |
| C | TurnID/PromptID | turn_id |
| D | 初始环境快照 | initial_snapshot |
| E | 轨迹文件 | 留空；上传轨迹后由用户填写 |
| F | 环境可复现等级 | reproducibility |
| G | Harness | harness |
| H | Harness 版本 | harness_version |
| I | 操作系统 | operating_system |
| J | 任务类型 | task_type |
| K | 任务难度 | difficulty |
| L | 语言/框架 | languages |
| M | 交付完整性 | delivery_score |
| N | 交付完整性 - 描述 | delivery_description |
| O | 指令遵循 | instruction_score |
| P | 指令遵循 - 描述 | instruction_description |
| Q | 任务规划 | planning_score |
| R | 任务规划 - 描述 | planning_description |
| S | 推理能力 | reasoning_score |
| T | 推理能力 - 描述 | reasoning_description |
| U | 执行能力 | execution_score |
| V | 执行能力 - 描述 | execution_description |
| W | 其他问题 | other_issues |
| X | 提交人 | submitter |
| Y | 提交时间 | submitted_at |
| Z | 父记录 | parent_record |
| AA | 审核备注 | delivery_qc_note |

Scores remain numeric. Identifiers, timestamps, prompts, descriptions, and parent IDs remain text.

`trajectory_file` remains required in SQLite for session validation and locating the original JSONL, but it is intentionally not written to column E. The exporter copies that JSONL unchanged into the batch root with the batch and question number in its file name.
