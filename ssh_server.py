#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import time
import asyncssh
from mcp.server.fastmcp import FastMCP

server = FastMCP("bot")


def _float_env(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default).strip())
    except (TypeError, ValueError):
        return float(default)


def _int_env(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default).strip())
    except (TypeError, ValueError):
        return int(default)


SSH_HOST = os.getenv("SSH_HOST", "").strip()
SSH_PORT = _int_env("SSH_PORT", "22")
SSH_USERNAME = os.getenv("SSH_USER", "").strip()
SSH_PASSWORD = os.getenv("SSH_PASSWORD", "").strip()
SSH_PRIVATE_KEY = os.getenv("SSH_PRIVATE_KEY", "").strip()  # 路径或私钥内容
SSH_KEY_PASSPHRASE = os.getenv("SSH_KEY_PASSPHRASE", "").strip()
SSH_KNOWN_HOSTS = os.getenv("SSH_KNOWN_HOSTS", "").strip()
SSH_CONNECT_TIMEOUT = _float_env("SSH_CONNECT_TIMEOUT", "10")
SSH_COMMAND_TIMEOUT = _float_env("SSH_COMMAND_TIMEOUT", "30")
# 连接空闲超过此秒数则关闭，下次调用时重连（0 表示不按空闲关闭，仅出错时重连）
SSH_IDLE_TIMEOUT = _float_env("SSH_IDLE_TIMEOUT", "60")

GLOBAL_ADMIN_IDS = {
    str(x).strip() for x in os.getenv("GLOBAL_ADMIN_IDS", "").split(",") if str(x).strip()
}
FALLBACK_OPERATOR_ID = os.getenv("FALLBACK_OPERATOR_ID", "").strip()

# 单次命令输出最大字符数，避免 token 溢出
MAX_OUTPUT_CHARS = max(100, _int_env("SSH_MAX_OUTPUT_CHARS", "8000"))

# 连接复用：全局长连接 + 锁（串行执行命令）
_ssh_conn: asyncssh.SSHClientConnection | None = None
_ssh_conn_lock = asyncio.Lock()
_ssh_last_used = 0.0

# 禁止执行的命令模式（子串匹配，小写）
BLOCKED_COMMAND_PATTERNS = (
    "rm -rf /",
    "mkfs.",
    ":(){ :|:& };:",  # fork bomb
    "dd if=",
    "> /dev/sd",
)


def _is_admin(uid: str) -> bool:
    uid = str(uid or "").strip()
    return bool(uid and uid in GLOBAL_ADMIN_IDS)


def _is_command_blocked(command: str) -> str | None:
    """若命令命中禁止规则则返回原因，否则返回 None。"""
    cmd_lower = (command or "").strip().lower()
    for pattern in BLOCKED_COMMAND_PATTERNS:
        if pattern in cmd_lower:
            return f"禁止执行包含 '{pattern}' 的命令"
    return None


def _check_base_config() -> str:
    if not SSH_HOST:
        return "❌ 未配置 SSH_HOST"
    if not SSH_USERNAME:
        return "❌ 未配置 SSH_USERNAME"
    if not SSH_PASSWORD and not SSH_PRIVATE_KEY:
        return "❌ SSH_PASSWORD 与 SSH_PRIVATE_KEY 至少配置一个"
    return ""


def _build_connect_kwargs():
    kwargs = {
        "host": SSH_HOST,
        "port": SSH_PORT,
        "username": SSH_USERNAME,
        "known_hosts": SSH_KNOWN_HOSTS if SSH_KNOWN_HOSTS else None,
        "login_timeout": SSH_CONNECT_TIMEOUT,
    }

    if SSH_PRIVATE_KEY:
        # 支持“私钥路径”或“私钥内容”
        if "BEGIN" in SSH_PRIVATE_KEY and "PRIVATE KEY" in SSH_PRIVATE_KEY:
            key_obj = asyncssh.import_private_key(
                SSH_PRIVATE_KEY,
                passphrase=SSH_KEY_PASSPHRASE or None
            )
            kwargs["client_keys"] = [key_obj]
        else:
            kwargs["client_keys"] = [SSH_PRIVATE_KEY]
            if SSH_KEY_PASSPHRASE:
                kwargs["passphrase"] = SSH_KEY_PASSPHRASE

        # 可选密码回退
        if SSH_PASSWORD:
            kwargs["password"] = SSH_PASSWORD
    else:
        kwargs["password"] = SSH_PASSWORD

    return kwargs


def _ensure_str(x):
    if x is None:
        return ""
    return x.decode("utf-8", errors="replace") if isinstance(x, bytes) else str(x)


def _truncate(text: str, max_len: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n\n... (输出已截断，共 {len(text)} 字符)"


async def _get_or_create_conn():
    """在锁内调用。返回当前可用连接，空闲超时或未连接时新建。"""
    global _ssh_conn, _ssh_last_used
    now = time.monotonic()
    if _ssh_conn is not None and SSH_IDLE_TIMEOUT > 0 and (now - _ssh_last_used) > SSH_IDLE_TIMEOUT:
        try:
            _ssh_conn.close()
            await _ssh_conn.wait_closed()
        except Exception:
            pass
        _ssh_conn = None
    if _ssh_conn is None:
        connect_kwargs = _build_connect_kwargs()
        _ssh_conn = await asyncio.wait_for(
            asyncssh.connect(**connect_kwargs),
            timeout=SSH_CONNECT_TIMEOUT
        )
    _ssh_last_used = time.monotonic()
    return _ssh_conn


async def _clear_conn():
    """在锁内调用。出错时清空连接以便下次重连。"""
    global _ssh_conn
    if _ssh_conn is not None:
        try:
            _ssh_conn.close()
            await _ssh_conn.wait_closed()
        except Exception:
            pass
        _ssh_conn = None


async def _run_ssh_command(command: str) -> str:
    global _ssh_conn
    async with _ssh_conn_lock:
        conn = await _get_or_create_conn()
        try:
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=SSH_COMMAND_TIMEOUT
            )
        except (asyncssh.ConnectionLost, asyncssh.DisconnectError, asyncssh.Error, OSError):
            await _clear_conn()
            raise
        except asyncio.TimeoutError:
            # 命令超时不关连接，只抛给上层
            raise

    stdout = _ensure_str(result.stdout).strip()
    stderr = _ensure_str(result.stderr).strip()

    if result.exit_status == 0:
        out = stdout if stdout else "✅ 命令执行成功（无输出）"
        return _truncate(out)
    return _truncate(
        f"❌ 命令执行失败\n"
        f"exit_code: {result.exit_status}\n"
        f"stderr: {stderr or '(empty)'}\n"
        f"stdout: {stdout or '(empty)'}"
    )


@server.tool()
async def execute_command(command: str, operator_id: str = "") -> str:
    """在已配置的 SSH 服务器上执行单条 shell 命令，并返回标准输出或错误信息。
    适用于查询系统状态（如 top、free、df）、查看日志等只读或低风险操作。
    command: 要执行的 shell 命令，例如 'top -b -n 1 | head -20'、'free -h'、'df -h'。
    operator_id: 调用方标识，需在 GLOBAL_ADMIN_IDS 中才有权限执行。
    """
    base_err = _check_base_config()
    if base_err:
        return base_err

    command = (command or "").strip()
    if not command:
        return "❌ command 不能为空"

    blocked = _is_command_blocked(command)
    if blocked:
        return f"🚫 {blocked}"

    operator_id = str(operator_id or FALLBACK_OPERATOR_ID).strip()
    if not operator_id:
        return "❌ 缺少 operator_id（且未配置 FALLBACK_OPERATOR_ID）"

    if not _is_admin(operator_id):
        return f"🚫 权限不足，operator_id={operator_id}"

    try:
        return await _run_ssh_command(command)
    except asyncio.TimeoutError:
        return f"⏱️ 超时（connect>{SSH_CONNECT_TIMEOUT}s 或 command>{SSH_COMMAND_TIMEOUT}s）"
    except asyncssh.PermissionDenied:
        return "❌ SSH 认证失败（用户名/密码/私钥错误）"
    except (asyncssh.ConnectionLost, asyncssh.DisconnectError) as e:
        return f"❌ SSH 连接断开：{e}"
    except asyncssh.Error as e:
        return f"❌ SSH 错误：{type(e).__name__}: {e}"
    except OSError as e:
        return f"❌ 网络或系统错误：{e}"
    except Exception as e:
        return f"❌ 未知错误：{type(e).__name__}: {e}"


if __name__ == "__main__":
    server.run()

