"""Interactive Reddit login and encrypted cookie persistence.

The login uses a dedicated Chrome profile and Chrome DevTools Protocol.  The
user enters credentials directly into Chrome; Reddit King only reads Reddit's
resulting cookies after a successful login.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

COOKIE_FILE_MAGIC = b"RKC1"
LOGIN_URL = "https://www.reddit.com/login/?dest=https%3A%2F%2Fold.reddit.com%2F"
REDDIT_LOGIN_COOKIE = "reddit_session"


def _app_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Reddit-King"
    return Path.home() / ".reddit-king"


def cookie_store_path() -> Path:
    return _app_data_dir() / "reddit-cookie.bin"


def chrome_profile_path() -> Path:
    return _app_data_dir() / "login-browser-profile"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect_for_current_user(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("持久化登录 Cookie 当前仅支持 Windows")
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(
        len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte))
    )
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        "Reddit King Cookie",
        None,
        None,
        None,
        0x01,
        ctypes.byref(target),
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _unprotect_for_current_user(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("持久化登录 Cookie 当前仅支持 Windows")
    source_buffer = ctypes.create_string_buffer(data)
    source = _DataBlob(
        len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte))
    )
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0x01, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def has_saved_reddit_login() -> bool:
    if not cookie_store_path().is_file():
        return False
    try:
        payload = load_reddit_login()
    except ValueError:
        return False
    return any(
        isinstance(item, dict) and item.get("name") == REDDIT_LOGIN_COOKIE
        for item in payload["cookies"]
    )


def save_reddit_login(payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encrypted = COOKIE_FILE_MAGIC + _protect_for_current_user(raw)
    path = cookie_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(encrypted)
    temporary.replace(path)


def load_reddit_login() -> dict[str, Any]:
    path = cookie_store_path()
    try:
        encrypted = path.read_bytes()
    except OSError as exc:
        raise ValueError("未找到已保存的 Reddit 登录 Cookie，请先点击预登录") from exc
    if not encrypted.startswith(COOKIE_FILE_MAGIC):
        raise ValueError("已保存的 Reddit 登录 Cookie 格式无效，请重新预登录")
    try:
        payload = json.loads(_unprotect_for_current_user(encrypted[4:]).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("无法读取已保存的 Reddit 登录 Cookie，请重新预登录") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cookies"), list):
        raise ValueError("已保存的 Reddit 登录 Cookie 格式无效，请重新预登录")
    return payload


def apply_saved_reddit_login(session: requests.Session) -> int:
    payload = load_reddit_login()
    applied = 0
    for item in payload["cookies"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        domain = str(item.get("domain") or "")
        if not name or not value or not domain.casefold().lstrip(".").endswith("reddit.com"):
            continue
        expires = item.get("expires")
        session.cookies.set(
            name,
            value,
            domain=domain,
            path=str(item.get("path") or "/"),
            secure=bool(item.get("secure", True)),
            expires=int(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
        )
        applied += 1
    user_agent = str(payload.get("user_agent") or "").strip()
    if user_agent:
        session.headers["User-Agent"] = user_agent
    if REDDIT_LOGIN_COOKIE not in session.cookies.keys():
        raise ValueError("已保存的 Reddit 登录已失效，请重新预登录")
    return applied


def _find_chrome() -> Path:
    candidates: list[Path] = []
    if os.name == "nt":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if os.environ.get(variable):
                candidates.append(
                    Path(os.environ[variable]) / "Google" / "Chrome" / "Application" / "chrome.exe"
                )
    elif sys.platform == "darwin":
        candidates.append(
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        )
    for command in ("google-chrome", "google-chrome-stable", "chrome"):
        executable = shutil.which(command)
        if executable:
            candidates.append(Path(executable))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("未找到 Google Chrome，请先安装 Chrome")


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


def _read_debug_json(url: str) -> dict[str, Any]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=2) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("Chrome 调试接口返回格式无效")
    return result


class _CdpSocket:
    def __init__(self, url: str, origin: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise RuntimeError("Chrome 调试接口地址无效")
        self.socket = socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 80), 5)
        self.socket.settimeout(5)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {parsed.path}?{parsed.query} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Origin: {origin}\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.socket.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 65536:
            response += self.socket.recv(4096)
        if not response.startswith(b"HTTP/1.1 101"):
            self.socket.close()
            raise RuntimeError("无法连接 Chrome 登录窗口")
        self._request_id = 0

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = bytes((0x80 | opcode, 0x80 | min(length, 127)))
        if length >= 126 and length <= 65535:
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", length)
        elif length > 65535:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def _receive_exact(self, length: int) -> bytes:
        result = bytearray()
        while len(result) < length:
            chunk = self.socket.recv(length - len(result))
            if not chunk:
                raise RuntimeError("Chrome 登录窗口连接已关闭")
            result.extend(chunk)
        return bytes(result)

    def _receive_text(self) -> str:
        chunks: list[bytes] = []
        while True:
            first, second = self._receive_exact(2)
            finished = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._receive_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._receive_exact(8))[0]
            mask = self._receive_exact(4) if second & 0x80 else b""
            payload = self._receive_exact(length)
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                raise RuntimeError("Chrome 登录窗口已关闭")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode in (0x0, 0x1):
                chunks.append(payload)
            if finished:
                return b"".join(chunks).decode("utf-8")

    def call(self, method: str) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        self._send_frame(json.dumps({"id": request_id, "method": method}).encode("utf-8"))
        while True:
            response = json.loads(self._receive_text())
            if response.get("id") != request_id:
                continue
            if response.get("error"):
                raise RuntimeError(str(response["error"].get("message") or "Chrome 调试失败"))
            result = response.get("result") or {}
            return result if isinstance(result, dict) else {}


def capture_and_save_reddit_login(timeout: int = 300) -> int:
    """Open Chrome, wait for a Reddit login, then securely replace saved cookies."""
    chrome = _find_chrome()
    port = _free_local_port()
    origin = f"http://127.0.0.1:{port}"
    profile = chrome_profile_path()
    profile.mkdir(parents=True, exist_ok=True)
    command = [
        str(chrome),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--remote-allow-origins={origin}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        LOGIN_URL,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    deadline = time.monotonic() + timeout
    debug_info: dict[str, Any] | None = None
    while time.monotonic() < deadline and process.poll() is None:
        try:
            debug_info = _read_debug_json(f"http://127.0.0.1:{port}/json/version")
            break
        except (OSError, ValueError, RuntimeError):
            time.sleep(0.25)
    if debug_info is None:
        raise RuntimeError("Chrome 登录窗口未能启动或已被关闭")

    cdp: _CdpSocket | None = None
    try:
        cdp = _CdpSocket(str(debug_info.get("webSocketDebuggerUrl") or ""), origin)
        while time.monotonic() < deadline and process.poll() is None:
            result = cdp.call("Storage.getCookies")
            cookies = [
                item
                for item in result.get("cookies", [])
                if isinstance(item, dict)
                and str(item.get("domain") or "").casefold().lstrip(".").endswith("reddit.com")
            ]
            names = {str(item.get("name") or "") for item in cookies}
            if REDDIT_LOGIN_COOKIE in names:
                save_reddit_login(
                    {
                        "saved_at": int(time.time()),
                        "user_agent": str(debug_info.get("User-Agent") or ""),
                        "cookies": cookies,
                    }
                )
                try:
                    cdp.call("Browser.close")
                except (OSError, RuntimeError):
                    pass
                return len(cookies)
            time.sleep(1)
    finally:
        if cdp is not None:
            if process.poll() is None:
                try:
                    cdp.call("Browser.close")
                except (OSError, RuntimeError):
                    process.terminate()
            cdp.close()
        elif process.poll() is None:
            process.terminate()
    raise RuntimeError("未检测到 Reddit 登录；原有 Cookie 未被修改")
