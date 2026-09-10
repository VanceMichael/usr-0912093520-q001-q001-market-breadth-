#!/usr/bin/env python3
"""Small authenticated client for the fixed SOLO2 delivery service."""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import subprocess
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from urllib.parse import urlparse

from tools.delivery_records import EXPORT_HEADERS, EXPORT_KEYS, SCORE_KEYS


SOLO2_ORIGIN = "https://solo2.jzxhnh.com"
API_ROOT = f"{SOLO2_ORIGIN}/api/v1"
CSRF_COOKIE = "solo_qa_csrf"
CSRF_HEADER = "X-CSRF-Token"
FILE_FIELD_TYPES = {"attachment", "file"}
if len(EXPORT_HEADERS) != len(EXPORT_KEYS):
    raise RuntimeError("SOLO2 field mapping does not match the export contract")
FIELD_LABEL_TO_KEY = dict(zip(EXPORT_HEADERS, EXPORT_KEYS))
REMOTE_KEY_ALIASES = {
    "env_snapshot": "initial_snapshot",
    "os_platform": "operating_system",
    "question_type": "task_type",
    "repro_level": "reproducibility",
    "trace_file": "trajectory_file",
    "x_iteration": "turn_no",
    "score_delivery": "delivery_score",
    "desc_delivery": "delivery_description",
    "score_instruction": "instruction_score",
    "desc_instruction": "instruction_description",
    "score_planning": "planning_score",
    "desc_planning": "planning_description",
    "score_reasoning": "reasoning_score",
    "desc_reasoning": "reasoning_description",
    "score_execution": "execution_score",
    "desc_execution": "execution_description",
}


class Solo2Error(RuntimeError):
    """A sanitized remote API error that never contains credentials or cookies."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _remote_error(payload: object, fallback: str) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("message") or payload.get("error")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        errors = payload.get("errors")
        if isinstance(errors, list):
            messages = [
                str(item.get("message", "")).strip()
                for item in errors if isinstance(item, dict)
            ]
            if any(messages):
                return "；".join(message for message in messages if message)
    return fallback


def _field_source_key(field: dict) -> str | None:
    key = str(field.get("field_key") or "").strip()
    if key in EXPORT_KEYS:
        return key
    if key in REMOTE_KEY_ALIASES:
        return REMOTE_KEY_ALIASES[key]
    label = str(field.get("label") or "").strip()
    return FIELD_LABEL_TO_KEY.get(label)


def map_record_to_schema(record: dict, schema: dict) -> tuple[dict, list[str]]:
    """Map one validated SQLite record to the server's current dynamic form schema."""
    fields = schema.get("fields")
    if not isinstance(fields, list) or not fields:
        raise Solo2Error("平台没有返回可用的提交表单字段")

    data: dict[str, object] = {}
    attachment_keys: list[str] = []
    problems: list[str] = []
    for raw_field in fields:
        if not isinstance(raw_field, dict) or raw_field.get("is_enabled") is False:
            continue
        field_key = str(raw_field.get("field_key") or "").strip()
        label = str(raw_field.get("label") or field_key).strip()
        if not field_key:
            problems.append("平台表单存在缺少 field_key 的字段")
            continue
        source_key = _field_source_key(raw_field)
        field_type = str(raw_field.get("field_type") or "").lower()
        required = bool(raw_field.get("is_required"))

        if field_type in FILE_FIELD_TYPES:
            if source_key == "trajectory_file":
                data[field_key] = []
                attachment_keys.append(field_key)
            elif required:
                problems.append(f"平台必填附件“{label}”没有对应的本地数据")
            else:
                data[field_key] = []
            continue

        if source_key is None:
            if required:
                problems.append(f"平台必填字段“{label}”无法映射到本地 28 字段")
            else:
                data[field_key] = ""
            continue

        value = record.get(source_key)
        if field_type == "number" or source_key in SCORE_KEYS:
            mapped: object = value
        else:
            mapped = "" if value is None else str(value)
        if source_key == "task_type" and isinstance(mapped, str):
            mapped = mapped.replace(" ", "")
        if required and (mapped is None or str(mapped).strip() == ""):
            problems.append(f"平台必填字段“{label}”为空")
        max_length = raw_field.get("max_length")
        min_length = raw_field.get("min_length")
        if isinstance(mapped, str):
            if isinstance(max_length, int) and max_length > 0 and len(mapped) > max_length:
                problems.append(f"字段“{label}”超过平台长度上限 {max_length}")
            if required and isinstance(min_length, int) and min_length > 0 and len(mapped) < min_length:
                problems.append(f"字段“{label}”少于平台长度下限 {min_length}")
        options = raw_field.get("options")
        if options and field_type in {"select", "radio"} and mapped not in options:
            problems.append(f"字段“{label}”的值不在平台当前选项中")
        data[field_key] = mapped
    if problems:
        raise Solo2Error("；".join(problems))
    return data, attachment_keys


class Solo2Client:
    def __init__(
        self, cookie_file: Path, timeout: float = 30, origin: str = SOLO2_ORIGIN,
    ) -> None:
        self.cookie_file = Path(cookie_file)
        self.timeout = timeout
        parsed = urlparse(origin.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise Solo2Error("SOLO2 地址必须是完整的 http(s) 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise Solo2Error("SOLO2 地址不能包含账号、密码、查询参数或片段")
        self.api_root = origin.rstrip("/") + "/api/v1"
        self.cookies = MozillaCookieJar(str(self.cookie_file))
        if self.cookie_file.is_file():
            try:
                self.cookies.load(ignore_discard=True, ignore_expires=False)
            except (OSError, ValueError):
                self.cookies.clear()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def _csrf_token(self) -> str:
        for cookie in self.cookies:
            if cookie.name == CSRF_COOKIE:
                return cookie.value
        return ""

    def _save_cookies(self) -> None:
        self.cookie_file.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.cookie_file.parent, 0o700)
        self.cookies.save(ignore_discard=True, ignore_expires=True)
        if os.name != "nt":
            os.chmod(self.cookie_file, 0o600)
        else:
            account = subprocess.run(
                ["whoami"], text=True, capture_output=True, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.strip()
            if not account:
                raise Solo2Error("平台已登录，但无法限制本机 Cookie 文件权限")
            secured = subprocess.run(
                ["icacls", str(self.cookie_file), "/inheritance:r", "/grant:r", f"{account}:F"],
                text=True, capture_output=True, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if secured.returncode:
                raise Solo2Error("平台已登录，但无法限制本机 Cookie 文件权限")

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> object:
        if not path.startswith("/") or path.startswith("//"):
            raise Solo2Error("平台接口路径无效")
        headers = {"Accept": "application/json"}
        if method != "GET":
            token = self._csrf_token()
            if token:
                headers[CSRF_HEADER] = token
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if body is not None:
            headers["Content-Type"] = content_type
        request = Request(self.api_root + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            raise Solo2Error(
                _remote_error(parsed, f"平台请求失败（HTTP {exc.code}）"), status=exc.code
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise Solo2Error("无法连接 SOLO2 平台，请检查网络后重试") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Solo2Error("平台返回了无法识别的数据") from exc

    def login(self, username: str, password: str) -> dict:
        username = username.strip()
        if not username or not password:
            raise Solo2Error("请输入平台账号和密码")
        self._request(
            "POST", "/auth/login", payload={"username": username, "password": password}
        )
        user = self.me()
        self._save_cookies()
        return user

    def me(self) -> dict:
        value = self._request("GET", "/auth/me")
        if not isinstance(value, dict):
            raise Solo2Error("平台返回的登录信息无效")
        return value

    def form_schema(self) -> dict:
        value = self._request("GET", "/submissions/form-schema")
        if not isinstance(value, dict):
            raise Solo2Error("平台返回的表单配置无效")
        return value

    def upload(self, path: Path) -> dict:
        path = Path(path)
        if not path.is_file():
            raise Solo2Error(f"找不到轨迹文件：{path.name}")
        boundary = "----ccusr" + secrets.token_hex(16)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8") + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode("ascii")
        value = self._request(
            "POST", "/submissions/upload", body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        if not isinstance(value, dict) or not value.get("path"):
            raise Solo2Error("轨迹文件上传成功，但平台返回的文件信息无效")
        return {
            "name": str(value.get("name") or path.name),
            "path": str(value["path"]),
            "size": int(value.get("size") or path.stat().st_size),
        }

    def create_submission(self, data: dict, schema_fingerprint: str) -> dict:
        value = self._request(
            "POST", "/submissions",
            payload={"data": data, "schema_fingerprint": schema_fingerprint},
        )
        if not isinstance(value, dict) or value.get("id") is None:
            raise Solo2Error("平台已接收请求，但没有返回提交编号")
        return value
