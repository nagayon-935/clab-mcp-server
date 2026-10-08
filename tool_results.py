"""Common MCP responses and bounded, resumable command output.

Output handles refer to completed command results in this server process. Reading
another page never executes a command again. The lab inventory remains stateless.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Literal

from typing_extensions import TypedDict


ResponseStatus = Literal["success", "partial", "error", "skipped"]


class ResultCounts(TypedDict):
    total: int
    succeeded: int
    failed: int
    skipped: int


class ToolResponse(TypedDict):
    tool: str
    status: ResponseStatus
    summary: str
    counts: ResultCounts
    data: dict[str, Any]
    warnings: list[str]
    errors: list[dict[str, Any]]
    next_steps: list[str]


class ToolInputError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def error_detail(exc: BaseException | str, node: str | None = None) -> dict[str, Any]:
    message = str(exc)
    kind = type(exc).__name__ if isinstance(exc, BaseException) else ""
    text = f"{kind}: {message}".lower()
    code, step = (
        "EXECUTION_FAILED",
        "diagnose_environment で実行環境を確認してください。",
    )
    if isinstance(exc, ToolInputError):
        code, step = exc.code, "引数と利用可能な対象を確認してください。"
    elif "authentication" in text or "permission denied (" in text:
        code, step = (
            "AUTHENTICATION_FAILED",
            "SSH鍵・ユーザー名・機器の認証設定を確認してください。",
        )
    elif (
        "timed out" in text
        or "timeout" in text
        or "タイムアウト" in text
        or "rc=124" in text
    ):
        code, step = "TIMEOUT", "宛先への到達性と機器の起動状態を確認してください。"
    elif "sudo:" in text:
        code, step = (
            "SUDO_FAILED",
            "ホストの非対話 sudo 権限と CLAB_SUDO を確認してください。",
        )
    elif "host key verification failed" in text:
        code, step = (
            "HOST_KEY_FAILED",
            "ホストのSSH鍵を確認し、known_hosts を更新してください。",
        )
    elif (
        "見つかりません" in text
        or "no such file" in text
        or "filenotfound" in text
        or "not found" in text
    ):
        code, step = (
            "NOT_FOUND",
            "実行ホストのバイナリ、またはローカルのファイルパスを確認してください。",
        )
    elif (
        "connection refused" in text
        or "no route to host" in text
        or "could not resolve" in text
    ):
        code, step = (
            "UNREACHABLE",
            "宛先、管理ネットワーク、SSHの踏み台設定を確認してください。",
        )
    elif isinstance(exc, ValueError):
        code, step = "INVALID_ARGUMENT", "引数の値と許可されたパスを確認してください。"
    return {
        "code": code,
        "message": message[:4000],
        "node": node,
        "next_step": step,
        "message_truncated": len(message) > 4000,
        "total_chars": len(message),
    }


def response(
    tool: str,
    summary: str,
    *,
    data: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    succeeded: int = 0,
    failed: int = 0,
    skipped: int = 0,
    status: ResponseStatus | None = None,
    next_steps: list[str] | None = None,
) -> ToolResponse:
    errors = errors or []
    if status is None:
        status = "partial" if failed and succeeded else "error" if failed else "success"
        if skipped and not succeeded and not failed:
            status = "skipped"
        elif skipped and succeeded:
            status = "partial"
    steps = list(
        dict.fromkeys([*(next_steps or []), *(e["next_step"] for e in errors)])
    )
    return {
        "tool": tool,
        "status": status,
        "summary": summary,
        "counts": {
            "total": succeeded + failed + skipped,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
        },
        "data": data or {},
        "warnings": warnings or [],
        "errors": errors,
        "next_steps": steps,
    }


def failure(
    tool: str, exc: BaseException | str, *, data: dict[str, Any] | None = None
) -> ToolResponse:
    error = error_detail(exc)
    return response(
        tool,
        f"操作に失敗しました: {error['message']}",
        data=data,
        errors=[error],
        failed=1,
    )


class OutputStore:
    """Bounded temporary output storage, private to one MCP process."""

    def __init__(self, ttl: float = 3600, max_entries: int = 100) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self._directory: tempfile.TemporaryDirectory | None = None
        self._entries: dict[str, float] = {}
        self._lock = threading.RLock()

    def _expire(self) -> None:
        now = time.monotonic()
        for handle, created in list(self._entries.items()):
            if now - created > self.ttl:
                self._remove(handle)

    def _remove(self, handle: str) -> None:
        if self._directory:
            try:
                os.unlink(os.path.join(self._directory.name, handle))
            except FileNotFoundError:
                pass
        self._entries.pop(handle, None)

    def save(self, text: str) -> str:
        with self._lock:
            return self._save(text)

    def _save(self, text: str) -> str:
        self._expire()
        while len(self._entries) >= self.max_entries:
            self._remove(next(iter(self._entries)))
        if self._directory is None:
            self._directory = tempfile.TemporaryDirectory(prefix="clab-mcp-output-")
        handle = uuid.uuid4().hex
        with open(
            os.path.join(self._directory.name, handle), "w", encoding="utf-8"
        ) as fh:
            fh.write(text)
        self._entries[handle] = time.monotonic()
        return handle

    def read(self, handle: str, offset: int, limit: int) -> dict[str, Any]:
        with self._lock:
            return self._read(handle, offset, limit)

    def _read(self, handle: str, offset: int, limit: int) -> dict[str, Any]:
        validate_output_limit(limit)
        if offset < 0:
            raise ToolInputError("INVALID_ARGUMENT", "offset は0以上で指定してください")
        self._expire()
        if handle not in self._entries or self._directory is None:
            raise ToolInputError(
                "OUTPUT_EXPIRED",
                "出力IDが存在しないか、期限切れです（保持は最大1時間・100件）",
            )
        with open(os.path.join(self._directory.name, handle), encoding="utf-8") as fh:
            text = fh.read()
        if offset > len(text):
            raise ToolInputError(
                "INVALID_ARGUMENT", "offset が出力の長さを超えています"
            )
        end = min(len(text), offset + limit)
        return {
            "output_id": handle,
            "output": text[offset:end],
            "offset": offset,
            "total_chars": len(text),
            "next_offset": end if end < len(text) else None,
            "truncated": end < len(text),
        }


def validate_output_limit(limit: int) -> None:
    if not 1 <= limit <= 100_000:
        raise ToolInputError(
            "INVALID_ARGUMENT", "max_output_chars は1〜100000で指定してください"
        )


OUTPUT_STORE = OutputStore()


def present_output(value: Any, limit: int) -> tuple[Any, dict[str, Any]]:
    validate_output_limit(limit)
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, ensure_ascii=False, default=str)
    )
    info: dict[str, Any] = {
        "truncated": False,
        "total_chars": len(text),
        "format": "text" if isinstance(value, str) else "json",
    }
    if len(text) <= limit:
        return value, info
    info.update(truncated=True, shown_chars=limit, next_offset=limit)
    if len(text) <= 1_000_000:
        try:
            info["output_id"] = OUTPUT_STORE.save(text)
        except OSError:
            info["unavailable_reason"] = (
                "出力の一時保存に失敗しました。コマンドの実行自体は完了しています。"
            )
    else:
        info["unavailable_reason"] = (
            "出力が保持上限（100万文字）を超えています。コマンド側で対象を絞ってください。"
        )
    return text[:limit], info
