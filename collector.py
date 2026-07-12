"""Keyword-focused Reddit dataset collector."""

from __future__ import annotations

import csv
import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from scraper.reddit_auth import apply_saved_reddit_login
from scraper.reddit_data import (
    ARCTIC_SHIFT_DEFAULT_BASE_URL,
    MAX_COMMENT_TREE_SIZE,
    OLD_REDDIT_DEFAULT_BASE_URL,
    fetch_arctic_comments,
    iter_reddit_search_pages,
)

POST_FIELDS = [
    "post_id",
    "subreddit",
    "search_keyword",
    "title",
    "author",
    "score",
    "body",
    "created_utc",
    "permalink",
    "url",
    "reported_comment_count",
    "collected_comment_count",
    "comments_complete",
]

COMMENT_FIELDS = [
    "post_id",
    "comment_id",
    "comment_url",
    "parent_id",
    "author",
    "score",
    "body",
    "created_utc",
    "depth",
]


def slugify(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_-]+", "_", value.strip()).strip("_")
    return slug[:60] or "keyword"


def iso_timestamp(value: Any) -> str:
    try:
        timestamp = float(value or 0)
    except (TypeError, ValueError):
        timestamp = 0
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat()


def _normalize_proxy_url(value: str) -> str:
    value = value.strip()
    if not value or value.casefold().startswith("socks"):
        return ""
    if "://" not in value:
        value = f"http://{value}"
    return value


def read_windows_proxy_settings() -> dict[str, str]:
    """Read the current user's enabled static WinINET HTTP proxy."""
    if os.name != "nt":
        return {}
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0] or 0)
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "").strip()
    except (OSError, TypeError, ValueError):
        return {}
    if not enabled or not server:
        return {}

    proxies: dict[str, str] = {}
    if "=" not in server:
        proxy_url = _normalize_proxy_url(server)
        if proxy_url:
            proxies.update({"http": proxy_url, "https": proxy_url})
        return proxies

    for entry in server.split(";"):
        protocol, separator, address = entry.partition("=")
        protocol = protocol.strip().casefold()
        if not separator or protocol not in {"http", "https"}:
            continue
        proxy_url = _normalize_proxy_url(address)
        if proxy_url:
            proxies[protocol] = proxy_url
    if "http" in proxies and "https" not in proxies:
        proxies["https"] = proxies["http"]
    return proxies


def safe_proxy_label(value: str) -> str:
    """Show a proxy endpoint without exposing embedded credentials."""
    parsed = urlsplit(value)
    if not parsed.hostname:
        return "configured"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme or 'http'}://{parsed.hostname}{port}"


def build_session(
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    *,
    use_saved_reddit_login: bool = False,
) -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "DNT": "1",
        }
    )
    session.cookies.set("over18", "1", domain=".reddit.com")
    saved_cookie_count = 0
    if use_saved_reddit_login:
        saved_cookie_count = apply_saved_reddit_login(session)
    session._reddit_saved_cookie_count = saved_cookie_count
    session.proxies.update(read_windows_proxy_settings())
    session.mount("https://", adapter)
    return session


def read_existing_ids(path: Path, field: str) -> set[str]:
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {
            str(row.get(field) or "")
            for row in csv.DictReader(handle)
            if row.get(field)
        }


def ensure_csv_file(path: Path, fields: list[str]) -> None:
    """Create a durable CSV header before network collection starts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        handle.flush()
        os.fsync(handle.fileno())


def append_rows(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    """Append one completed collection batch and force it to disk."""
    ensure_csv_file(path, fields)
    if not rows:
        return
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(slots=True)
class CollectorConfig:
    subreddit: str | None
    keyword: str
    post_limit: int = 0
    output_dir: Path | None = None
    after: str | None = None
    before: str | None = None
    collect_comments: bool = True
    all_comments: bool = True
    minimum_comment_capacity: int = 1000
    max_comment_depth: int = 20
    max_pages: int = 0
    search_sort: str = "relevance"
    request_delay: float = 0.75
    search_base_url: str = OLD_REDDIT_DEFAULT_BASE_URL
    comment_base_url: str = ARCTIC_SHIFT_DEFAULT_BASE_URL
    use_saved_reddit_login: bool = False

    def __post_init__(self) -> None:
        self.subreddit = (self.subreddit or "").removeprefix("r/").strip()
        self.keyword = self.keyword.strip()
        if not self.keyword:
            raise ValueError("keyword cannot be empty")
        if self.post_limit < 0:
            raise ValueError("post_limit cannot be negative; use 0 for unlimited")
        if not 1 <= self.minimum_comment_capacity <= MAX_COMMENT_TREE_SIZE:
            raise ValueError("minimum_comment_capacity must be between 1 and 25000")
        if self.max_comment_depth < 0:
            raise ValueError("max_comment_depth cannot be negative")
        if self.max_pages < 0:
            raise ValueError("max_pages cannot be negative; use 0 for unlimited")
        if self.search_sort not in {"relevance", "new", "comments", "top", "hot"}:
            raise ValueError("invalid search_sort")
        if self.output_dir is None:
            target_slug = self.subreddit or "all"
            timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            self.output_dir = Path("output") / f"{timestamp}_{target_slug}_{slugify(self.keyword)}"
        else:
            self.output_dir = Path(self.output_dir)


class KeywordCollector:
    def __init__(
        self,
        config: CollectorConfig,
        *,
        session: requests.Session | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.session = session or build_session(
            use_saved_reddit_login=config.use_saved_reddit_login
        )
        self.log = log
        self.posts_path = config.output_dir / "posts.csv"
        self.comments_path = config.output_dir / "comments.csv"
        self.existing_post_ids = read_existing_ids(self.posts_path, "post_id")
        self.existing_comment_ids = read_existing_ids(self.comments_path, "comment_id")

    def _emit(self, message: str) -> None:
        timestamp = dt.datetime.now().strftime("%H:%M:%S")
        self.log(f"[{timestamp}] {message}")

    def _comment_limit(self, reported_count: int) -> int:
        if not self.config.collect_comments or reported_count <= 0:
            return 0
        if self.config.all_comments:
            desired = max(reported_count, self.config.minimum_comment_capacity)
        else:
            desired = self.config.minimum_comment_capacity
        return min(desired, MAX_COMMENT_TREE_SIZE)

    def _post_row(
        self,
        raw: dict[str, Any],
        collected_comments: int,
    ) -> dict[str, Any]:
        post_id = str(raw.get("id") or "")
        subreddit = str(raw.get("subreddit") or self.config.subreddit)
        reported = int(raw.get("num_comments") or 0)
        return {
            "post_id": post_id,
            "subreddit": subreddit,
            "search_keyword": self.config.keyword,
            "title": raw.get("title") or "",
            "author": raw.get("author") or "",
            "score": int(raw.get("score") or 0),
            "body": raw.get("selftext") or "",
            "created_utc": iso_timestamp(raw.get("created_utc")),
            "permalink": f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/",
            "url": raw.get("url") or "",
            "reported_comment_count": reported,
            "collected_comment_count": collected_comments,
            "comments_complete": collected_comments >= reported,
        }

    def run(self) -> dict[str, Any]:
        cfg = self.config
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        # Make the result files visible immediately. Subsequent rows are
        # appended and fsync'd after every completed post, so an interruption
        # only affects the item currently being requested.
        ensure_csv_file(self.posts_path, POST_FIELDS)
        ensure_csv_file(self.comments_path, COMMENT_FIELDS)
        limit_label = str(cfg.post_limit) if cfg.post_limit else "无限（直到结果耗尽）"
        pages_label = str(cfg.max_pages) if cfg.max_pages else "无限（直到数据源结束）"
        target_label = f"r/{cfg.subreddit}" if cfg.subreddit else "全 Reddit"
        sort_labels = {
            "relevance": "关联性",
            "new": "最新",
            "comments": "评论最多",
            "top": "最高得分",
            "hot": "热门",
        }
        self._emit(f"搜索范围: {target_label}")
        self._emit(f"搜索关键词: {cfg.keyword}")
        self._emit("搜索来源: old.reddit.com 旧版 HTML（无需 Reddit API Key）")
        saved_cookie_count = int(getattr(self.session, "_reddit_saved_cookie_count", 0))
        if cfg.use_saved_reddit_login:
            self._emit(f"Reddit 登录: 本次已加载保存的 Cookie（{saved_cookie_count} 项）")
        else:
            self._emit("Reddit 登录: 本次不使用保存的 Cookie（匿名访问）")
        self._emit(f"搜索排序: {sort_labels[cfg.search_sort]} ({cfg.search_sort})")
        if self.session.proxies:
            proxy_labels = ", ".join(
                f"{protocol}={safe_proxy_label(address)}"
                for protocol, address in sorted(self.session.proxies.items())
            )
            self._emit(f"网络代理: 已显式采用 Windows 用户代理设置 | {proxy_labels}")
        else:
            self._emit("网络代理: 未检测到已启用的 Windows 静态 HTTP 代理")
        self._emit(f"帖子保存上限: {limit_label}")
        self._emit(f"搜索页上限: {pages_label}，每页最多 100 条")
        if not cfg.collect_comments:
            self._emit("评论策略: 本次不采集评论")
        else:
            self._emit(
                "评论策略: "
                + (
                    "尽量抓全，按 Reddit 评论计数请求，单帖最高 25000"
                    if cfg.all_comments
                    else f"固定单帖上限 {cfg.minimum_comment_capacity}"
                )
            )
        self._emit(f"输出目录: {cfg.output_dir.resolve()}")
        self._emit("实时落盘: 已启用（每处理完一篇帖子立即追加并强制写入 CSV）")

        saved_posts = 0
        saved_comments = 0
        scanned_posts = 0
        incomplete_threads = 0

        for page_number, raw_page in enumerate(
            iter_reddit_search_pages(
                self.session,
                cfg.subreddit,
                cfg.keyword,
                after=cfg.after,
                before=cfg.before,
                max_pages=cfg.max_pages,
                sort=cfg.search_sort,
                base_url=cfg.search_base_url,
                delay_seconds=cfg.request_delay,
                log=self._emit,
            ),
            start=1,
        ):
            self._emit(f"开始处理搜索页 {page_number} 的 {len(raw_page)} 条搜索结果")
            for raw_post in raw_page:
                if cfg.post_limit and saved_posts >= cfg.post_limit:
                    break
                scanned_posts += 1
                post_id = str(raw_post.get("id") or "")
                if not post_id or post_id in self.existing_post_ids:
                    continue

                reported_comments = int(raw_post.get("num_comments") or 0)
                comment_limit = self._comment_limit(reported_comments)
                comments: list[dict[str, Any]] = []
                title_preview = str(raw_post.get("title") or "")[:80]
                target_label = str(cfg.post_limit) if cfg.post_limit else "∞"
                subreddit = str(raw_post.get("subreddit") or cfg.subreddit or "")
                post_url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"
                self._emit(f"{'─' * 22} 帖子 {saved_posts + 1}/{target_label} {'─' * 22}")
                self._emit(
                    f"ID {post_id} | 评分 {int(raw_post.get('score') or 0)} points | "
                    f"Reddit 评论 {reported_comments} 条 | 标题: {title_preview}"
                )
                self._emit(f"帖子链接 | {post_url}")
                if comment_limit:
                    self._emit(
                        f"评论请求 | Reddit 评论 {reported_comments} 条 | "
                        f"本次请求上限 {comment_limit} | 深度 {cfg.max_comment_depth}"
                    )
                    try:
                        comments = fetch_arctic_comments(
                            self.session,
                            post_id,
                            max_comments=comment_limit,
                            max_depth=cfg.max_comment_depth,
                            base_url=cfg.comment_base_url,
                        )
                        self._emit(
                            f"评论返回 | 实际 {len(comments)} 条 | "
                            f"唯一 ID {len({item['comment_id'] for item in comments})}"
                        )
                    except requests.RequestException as exc:
                        self._emit(f"评论抓取失败 | 帖子 {post_id} | {exc}")
                        comments = []
                elif cfg.collect_comments:
                    self._emit("评论跳过 | Reddit 评论 0 条，没有可采集评论")

                new_comments = [
                    comment
                    for comment in comments
                    if comment.get("comment_id")
                    and str(comment["comment_id"]) not in self.existing_comment_ids
                ]
                for comment in new_comments:
                    comment_id = str(comment["comment_id"])
                    comment["score"] = int(comment.get("score") or 0)
                    if not comment.get("comment_url"):
                        comment["comment_url"] = (
                            f"https://www.reddit.com/comments/{post_id}/_/{comment_id}/"
                        )
                append_rows(self.comments_path, COMMENT_FIELDS, new_comments)
                for comment in new_comments:
                    self.existing_comment_ids.add(str(comment["comment_id"]))
                if new_comments:
                    self._emit(
                        f"评论保存 | {len(new_comments)} 条完整数据已写入 comments.csv | "
                        f"示例链接: {new_comments[0].get('comment_url', '')}"
                    )

                post_row = self._post_row(raw_post, len(comments))
                append_rows(self.posts_path, POST_FIELDS, [post_row])
                self.existing_post_ids.add(post_id)
                saved_posts += 1
                saved_comments += len(new_comments)
                if cfg.collect_comments and not post_row["comments_complete"]:
                    incomplete_threads += 1
                target_label = str(cfg.post_limit) if cfg.post_limit else "∞"
                if not cfg.collect_comments:
                    comment_progress = "评论采集未启用"
                elif reported_comments == 0:
                    comment_progress = "Reddit 评论 0 条，保存 0 条"
                else:
                    comment_progress = (
                        f"Reddit 评论 {reported_comments} 条，返回 {len(comments)} 条，"
                        f"本次新增保存 {len(new_comments)} 条"
                    )
                self._emit(
                    f"落盘进度 | 帖子 {saved_posts}/{target_label} | {comment_progress} | "
                    f"累计保存评论 {saved_comments} | "
                    f"不完整评论树 {incomplete_threads} | 已写入磁盘"
                )

            if cfg.post_limit and saved_posts >= cfg.post_limit:
                break

        summary = {
            "posts_saved": saved_posts,
            "comments_saved": saved_comments,
            "posts_scanned": scanned_posts,
            "incomplete_threads": incomplete_threads,
            "posts_file": str(self.posts_path.resolve()),
            "comments_file": str(self.comments_path.resolve()),
        }
        self._emit(
            f"完成：帖子 {saved_posts}，新增评论 {saved_comments}，"
            f"未完全匹配 Reddit 评论计数的评论树 {incomplete_threads}"
        )
        return summary
