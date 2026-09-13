#!/usr/bin/env python3
"""Shared scope constraints for question-authoring entry points."""

from __future__ import annotations


BACKEND_LANGUAGES = (
    "Go",
    "Python",
    "Node.js（JavaScript 或 TypeScript）",
    "Java",
)


def sqlite_only_requirement() -> str:
    """Return the storage boundary for automatically authored batches."""
    return (
        "自动出题批次只允许使用 SQLite 作为持久化存储；不得使用 PostgreSQL、Redis、"
        "MongoDB、MySQL 或其他外部数据库和缓存服务。数据库文件必须位于当前题目工作目录"
        "或由环境变量配置，必须提供初始化/迁移命令和自动化测试命令，不得依赖共享服务、"
        "固定账号、固定端口或外部网络。"
    )


def backend_only_requirement() -> str:
    languages = "、".join(BACKEND_LANGUAGES)
    return (
        f"本批次只允许纯后端项目，主要后端语言或运行时只能从 {languages} 中选择，"
        "每道题选择一种与业务场景匹配的主要后端技术栈，并在批次内保持合理多样性。"
        "可以包含 HTTP/RPC API、消息处理、数据库、缓存、定时任务、并发控制、可靠性、"
        "安全边界、可观测性及后端自动化测试，但不得要求或创建任何前端页面、管理后台界面、"
        "浏览器客户端、HTML/CSS、React、Vue、Angular、Svelte、小程序或其他可视化 UI；"
        "不得生成全栈题，也不得用一个空壳前端包装后端任务。验收必须通过后端接口、测试、"
        "命令或可核验的持久化状态完成，不依赖浏览器操作。"
    )
