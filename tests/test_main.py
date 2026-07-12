from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import requests

import main


def test_report_http_403_is_concise_and_preserves_details(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    response = SimpleNamespace(
        status_code=403,
        url="https://old.reddit.com/search?q=gta6",
        headers={"Content-Type": "text/html"},
        content=b"<html><body>whoa there, pardner!</body></html>",
        request=SimpleNamespace(headers={"User-Agent": "Reddit-King/test"}),
    )
    session = requests.Session()
    monkeypatch.setattr(main, "fetch_public_ip", lambda _session: ("203.0.113.10", None))

    main.report_http_error(response, tmp_path, session)

    stderr = capsys.readouterr().err
    assert stderr == (
        "Reddit 返回 HTTP 403；当前公网 IP：203.0.113.10；"
        "错误详情已保存到日志；请更换 VPN 节点后重试。\n"
    )
    assert "whoa there" not in stderr
    assert (tmp_path / "reddit-http-error-403.html").read_bytes() == response.content

    diagnostic = json.loads(
        (tmp_path / "reddit-http-diagnostic.json").read_text(encoding="utf-8-sig")
    )
    assert diagnostic["status_code"] == 403
    assert diagnostic["public_ip"] == "203.0.113.10"
