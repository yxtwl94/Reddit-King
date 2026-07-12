"""Reddit data sources used by Reddit King.

Post discovery parses old.reddit.com's public HTML search pages, which need no
OAuth application or API key. Arctic Shift is retained only for large comment
trees because Reddit's HTML collapses most comments on busy threads.
"""

from __future__ import annotations

import datetime as dt
import re
import time
from collections.abc import Iterable, Iterator
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from bs4 import BeautifulSoup

ARCTIC_SHIFT_DEFAULT_BASE_URL = "https://arctic-shift.photon-reddit.com"
OLD_REDDIT_DEFAULT_BASE_URL = "https://old.reddit.com"
MAX_COMMENT_TREE_SIZE = 25_000
MAX_SEARCH_PAGE_SIZE = 100


def _reddit_number(text: str) -> int:
    """Parse old Reddit counters such as ``920`` or ``1.2k``."""
    match = re.search(r"(-?[\d,.]+)\s*([kKmM]?)", text)
    if not match:
        return 0
    value = float(match.group(1).replace(",", ""))
    suffix = match.group(2).casefold()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def _iso_timestamp(value: Any) -> str:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError):
        timestamp = 0
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat()


def _comment_record(data: dict[str, Any], post_id: str, depth: int) -> dict[str, Any]:
    comment_id = str(data.get("id") or "")
    return {
        "post_id": post_id,
        "comment_id": comment_id,
        "comment_url": f"https://www.reddit.com/comments/{post_id}/_/{comment_id}/",
        "parent_id": data.get("parent_id"),
        "author": data.get("author"),
        "score": int(data.get("score") or 0),
        "body": data.get("body", ""),
        "created_utc": _iso_timestamp(data.get("created_utc")),
        "depth": data.get("depth", depth),
    }


def parse_comment_items(
    items: Iterable[dict[str, Any]],
    post_id: str,
    *,
    max_depth: int = 20,
    max_comments: int = 1000,
) -> list[dict[str, Any]]:
    """Flatten an Arctic Shift/Reddit style comment tree without duplicates."""
    comments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def visit(nodes: Iterable[dict[str, Any]], depth: int) -> None:
        if depth > max_depth or len(comments) >= max_comments:
            return
        for item in nodes:
            if len(comments) >= max_comments:
                return
            if not isinstance(item, dict) or item.get("kind") != "t1":
                continue
            data = item.get("data") or {}
            comment_id = str(data.get("id") or "")
            if comment_id and comment_id not in seen_ids:
                seen_ids.add(comment_id)
                comments.append(_comment_record(data, post_id, depth))

            replies = data.get("replies")
            if replies and isinstance(replies, dict):
                visit(replies.get("data", {}).get("children", []), depth + 1)

    visit(items, 0)
    return comments


def fetch_arctic_comments(
    session: Any,
    post_id: str,
    *,
    max_comments: int = 1000,
    max_depth: int = 20,
    base_url: str = ARCTIC_SHIFT_DEFAULT_BASE_URL,
    timeout: int = 120,
) -> list[dict[str, Any]]:
    """Fetch a flattened comment tree, capped by Arctic Shift at 25,000."""
    if max_comments <= 0:
        return []
    request_limit = min(max_comments, MAX_COMMENT_TREE_SIZE)
    url = f"{base_url.rstrip('/')}/api/comments/tree"
    params = {
        "link_id": f"t3_{post_id}",
        "limit": request_limit,
        "start_depth": max_depth,
        # Values above 100 can cause 422 on large threads.
        "start_breadth": min(request_limit, 100),
    }
    response = session.get(url, params=params, timeout=timeout)
    if getattr(response, "status_code", None) == 422 and max_depth > 10:
        # Large threads can reject deep expansion even when the documented
        # values are otherwise valid. Depth 10 reliably preserves large trees.
        params["start_depth"] = 10
        response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return parse_comment_items(
        response.json().get("data", []),
        post_id,
        max_depth=max_depth,
        max_comments=request_limit,
    )


def parse_old_reddit_search_html(
    html: str,
    *,
    base_url: str = OLD_REDDIT_DEFAULT_BASE_URL,
) -> tuple[list[dict[str, Any]], str | None]:
    """Extract post records and the ``after`` cursor from an old Reddit page."""
    soup = BeautifulSoup(html, "html.parser")
    posts: list[dict[str, Any]] = []

    for result in soup.select("div.search-result-link[data-fullname]"):
        fullname = str(result.get("data-fullname") or "")
        if not fullname.startswith("t3_"):
            continue
        post_id = fullname.removeprefix("t3_")
        title_node = result.select_one("a.search-title")
        if title_node is None:
            continue

        title = title_node.get_text(" ", strip=True)
        href = urljoin(base_url, str(title_node.get("href") or ""))
        path = urlsplit(href).path
        author_node = result.select_one("a.author")
        subreddit_node = result.select_one("a.search-subreddit-link")
        time_node = result.select_one("time[datetime]")
        body_node = result.select_one("div.search-result-body div.md")
        comments_node = result.select_one("a.search-comments")
        score_node = result.select_one("span.search-score")

        subreddit = ""
        if subreddit_node is not None:
            subreddit = subreddit_node.get_text(" ", strip=True).removeprefix("r/")

        created_utc = 0.0
        if time_node is not None:
            date_text = str(time_node.get("datetime") or "")
            try:
                created_utc = dt.datetime.fromisoformat(date_text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                created_utc = 0.0

        num_comments = 0
        if comments_node is not None:
            count_match = re.search(r"[\d,]+", comments_node.get_text(" ", strip=True))
            if count_match:
                num_comments = int(count_match.group(0).replace(",", ""))

        posts.append(
            {
                "id": post_id,
                "subreddit": subreddit,
                "title": title,
                "author": author_node.get_text(" ", strip=True) if author_node else "",
                "score": _reddit_number(score_node.get_text(" ", strip=True)) if score_node else 0,
                "selftext": body_node.get_text("\n", strip=True) if body_node else "",
                "created_utc": created_utc,
                "permalink": path,
                "url": href,
                "num_comments": num_comments,
            }
        )

    next_cursor: str | None = None
    next_node = soup.select_one('a[rel~="next"]')
    if next_node is not None:
        next_query = parse_qs(urlsplit(str(next_node.get("href") or "")).query)
        next_cursor = (next_query.get("after") or [None])[0]
    return posts, next_cursor


def _date_epoch(value: str | int | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except ValueError as exc:
        raise ValueError(f"invalid date: {value}") from exc


def iter_reddit_search_pages(
    session: Any,
    subreddit: str | None,
    keyword: str,
    *,
    after: str | int | None = None,
    before: str | int | None = None,
    max_pages: int = 0,
    sort: str = "relevance",
    page_size: int = 100,
    base_url: str = OLD_REDDIT_DEFAULT_BASE_URL,
    timeout: int = 60,
    delay_seconds: float = 0.75,
    log: Callable[[str], None] | None = None,
) -> Iterator[list[dict[str, Any]]]:
    """Yield posts from old Reddit HTML search using its ``after`` cursor."""
    page_size = max(1, min(page_size, MAX_SEARCH_PAGE_SIZE))
    if sort not in {"relevance", "new", "comments", "top", "hot"}:
        raise ValueError(f"invalid Reddit search sort: {sort}")
    after_epoch = _date_epoch(after)
    before_epoch = _date_epoch(before)
    cursor: str | None = None
    seen_ids: set[str] = set()

    page_number = 0
    while max_pages == 0 or page_number < max_pages:
        page_number += 1
        path = f"/r/{quote(subreddit, safe='')}/search" if subreddit else "/search"
        params: dict[str, Any] = {
            "q": keyword,
            "include_over_18": "on",
            "limit": page_size,
            "sort": sort,
            "t": "all",
            "type": "link",
        }
        if subreddit:
            params["restrict_sr"] = "on"
        if cursor:
            params["after"] = cursor
            params["count"] = (page_number - 1) * page_size

        response = session.get(
            f"{base_url.rstrip('/')}{path}",
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        raw_page, next_cursor = parse_old_reddit_search_html(response.text, base_url=base_url)
        if not raw_page:
            text = response.text.casefold()
            if "making sure you're not a bot" in text or "you've been blocked" in text:
                raise ValueError("old Reddit returned an anti-bot challenge page")
            break

        page: list[dict[str, Any]] = []
        timestamps: list[float] = []
        for post in raw_page:
            post_id = str(post.get("id") or "")
            created_utc = float(post.get("created_utc") or 0)
            if created_utc:
                timestamps.append(created_utc)
            if before_epoch is not None and created_utc > before_epoch:
                continue
            if after_epoch is not None and created_utc < after_epoch:
                continue
            if post_id and post_id not in seen_ids:
                seen_ids.add(post_id)
                page.append(post)

        if log:
            log(
                f"old Reddit 搜索页 {page_number}: 页面返回 {len(raw_page)} 条，"
                f"日期过滤/去重后保留 {len(page)} 条，下一页游标 {next_cursor or '无'}"
            )

        if page:
            yield page
        if after_epoch is not None and timestamps and min(timestamps) < after_epoch:
            break
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

        if delay_seconds > 0 and (max_pages == 0 or page_number < max_pages):
            time.sleep(delay_seconds)
