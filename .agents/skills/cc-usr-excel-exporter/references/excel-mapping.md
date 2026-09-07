# Excel column mapping

The `数据表` worksheet has exactly 25 exported columns:

| Column | Header | SQLite column |
| --- | --- | --- |
| A | User Prompt | user_prompt |
| B | SessionID | session_id |
| C | TurnID/PromptID | turn_id |
| D | 初始环境快照 | initial_snapshot |
| E | 环境可复现等级 | reproducibility |
| F | Harness | harness |
| G | Harness 版本 | harness_version |
| H | 操作系统 | operating_system |
| I | 任务类型 | task_type |
| J | 任务难度 | difficulty |
| K | 语言/框架 | languages |
| L | 交付完整性 | delivery_score |
| M | 交付完整性 - 描述 | delivery_description |
| N | 指令遵循 | instruction_score |
| O | 指令遵循 - 描述 | instruction_description |
| P | 任务规划 | planning_score |
| Q | 任务规划 - 描述 | planning_description |
| R | 推理能力 | reasoning_score |
| S | 推理能力 - 描述 | reasoning_description |
| T | 执行能力 | execution_score |
| U | 执行能力 - 描述 | execution_description |
| V | 其他问题 | other_issues |
| W | 提交人 | submitter |
| X | 提交时间 | submitted_at |
| Y | 父记录 | parent_record |

Scores remain numeric. Identifiers, timestamps, prompts, descriptions, and parent IDs remain text.
