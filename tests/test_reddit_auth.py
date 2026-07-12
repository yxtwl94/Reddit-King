from __future__ import annotations

from pathlib import Path

import requests

from scraper import reddit_auth


def test_saved_login_round_trip_and_apply(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "reddit-cookie.bin"
    monkeypatch.setattr(reddit_auth, "cookie_store_path", lambda: store)
    monkeypatch.setattr(reddit_auth, "_protect_for_current_user", lambda data: data[::-1])
    monkeypatch.setattr(reddit_auth, "_unprotect_for_current_user", lambda data: data[::-1])
    payload = {
        "user_agent": "Chrome test",
        "cookies": [
            {
                "name": "reddit_session",
                "value": "secret-session-value",
                "domain": ".reddit.com",
                "path": "/",
                "secure": True,
                "expires": 2_000_000_000,
            },
            {
                "name": "unrelated",
                "value": "ignored",
                "domain": ".example.com",
                "path": "/",
            },
        ],
    }

    reddit_auth.save_reddit_login(payload)

    assert store.read_bytes().startswith(reddit_auth.COOKIE_FILE_MAGIC)
    assert b"secret-session-value" not in store.read_bytes()
    session = requests.Session()
    assert reddit_auth.apply_saved_reddit_login(session) == 1
    assert session.cookies.get("reddit_session", domain=".reddit.com") == "secret-session-value"
    assert session.headers["User-Agent"] == "Chrome test"


def test_invalid_saved_login_is_not_deleted(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "reddit-cookie.bin"
    store.write_bytes(b"invalid")
    monkeypatch.setattr(reddit_auth, "cookie_store_path", lambda: store)

    try:
        reddit_auth.load_reddit_login()
    except ValueError:
        pass
    else:
        raise AssertionError("invalid saved login should fail")

    assert store.read_bytes() == b"invalid"


def test_anonymous_token_is_not_accepted_as_login(tmp_path: Path, monkeypatch) -> None:
    store = tmp_path / "reddit-cookie.bin"
    monkeypatch.setattr(reddit_auth, "cookie_store_path", lambda: store)
    monkeypatch.setattr(reddit_auth, "_protect_for_current_user", lambda data: data)
    monkeypatch.setattr(reddit_auth, "_unprotect_for_current_user", lambda data: data)
    reddit_auth.save_reddit_login(
        {
            "cookies": [
                {
                    "name": "token_v2",
                    "value": "anonymous-token",
                    "domain": ".reddit.com",
                    "path": "/",
                }
            ]
        }
    )

    try:
        reddit_auth.apply_saved_reddit_login(requests.Session())
    except ValueError:
        pass
    else:
        raise AssertionError("anonymous token_v2 must not count as a Reddit login")
