"""Reddit King command-line entry point."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

from collector import CollectorConfig, KeywordCollector, safe_proxy_label

PUBLIC_IP_URL = "https://api.ipify.org?format=json"
PROXY_ENV_NAMES = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}


def fetch_public_ip(session: requests.Session) -> tuple[str | None, str | None]:
    """Return the public IP used by the collector's requests session."""
    try:
        response = session.get(PUBLIC_IP_URL, timeout=10)
        response.raise_for_status()
        public_ip = str(response.json().get("ip") or "").strip()
        ipaddress.ip_address(public_ip)
        return public_ip, None
    except (KeyError, ValueError, requests.RequestException) as exc:
        return None, str(exc)


def report_http_error(
    response: Any,
    output_dir: Path,
    session: requests.Session | None = None,
) -> None:
    """Preserve an HTTP failure response and print a concise user-facing message."""
    if response is None:
        return

    status_code = getattr(response, "status_code", "unknown")
    url = getattr(response, "url", "")
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("Content-Type") or "unknown")
    content = bytes(getattr(response, "content", b"") or b"")
    request = getattr(response, "request", None)
    request_headers = getattr(request, "headers", {}) or {}
    if not request_headers and session is not None:
        request_headers = session.headers
    user_agent = str(request_headers.get("User-Agent") or "（未发送）")
    accept = str(request_headers.get("Accept") or "（未发送）")
    accept_language = str(request_headers.get("Accept-Language") or "（未发送）")
    proxy_env_names = sorted(
        {
            name.upper()
            for name, value in os.environ.items()
            if name.upper() in PROXY_ENV_NAMES and value
        }
    )
    configured_proxies = (
        {
            protocol: safe_proxy_label(address)
            for protocol, address in sorted(session.proxies.items())
        }
        if session is not None
        else {}
    )

    public_ip: str | None = None
    public_ip_error: str | None = None
    if session is not None:
        public_ip, public_ip_error = fetch_public_ip(session)

    if "html" in content_type.casefold():
        suffix = ".html"
    elif "json" in content_type.casefold():
        suffix = ".json"
    else:
        suffix = ".txt"

    details_saved = False
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        response_path = output_dir / f"reddit-http-error-{status_code}{suffix}"
        response_path.write_bytes(content)

        diagnostic_path = output_dir / "reddit-http-diagnostic.json"
        diagnostic_path.write_text(
            json.dumps(
                {
                    "status_code": status_code,
                    "url": url,
                    "content_type": content_type,
                    "response_bytes": len(content),
                    "public_ip": public_ip,
                    "public_ip_lookup_error": public_ip_error,
                    "request_headers": {
                        "User-Agent": user_agent,
                        "Accept": accept,
                        "Accept-Language": accept_language,
                    },
                    "proxy_environment_variables": proxy_env_names,
                    "configured_session_proxies": configured_proxies,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8-sig",
        )
        details_saved = True
    except OSError as exc:
        print(f"HTTP 响应/诊断信息保存失败：{exc}", file=sys.stderr)

    message = f"Reddit 返回 HTTP {status_code}；当前公网 IP：{public_ip or '查询失败'}"
    if details_saved:
        message += "；错误详情已保存到日志"
    if status_code == 403:
        message += "；请更换 VPN 节点后重试"
    print(message + "。", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reddit King：按关键词采集帖子和评论",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  uv run --frozen python main.py -s python -k "local AI" --limit 100
  uv run --frozen python main.py -s AskReddit -k "career OR job" --limit 500
  uv run --frozen python main.py -s python -k asyncio --after 2024-01-01 --before 2025-01-01

搜索结果以 Reddit 返回为准。默认按 Reddit 评论计数尽量抓全，且每帖请求容量至少 1000，
Arctic Shift 单帖最多返回 25000 条评论。
""",
    )
    parser.add_argument("-s", "--subreddit", default="", help="可选：限定 Subreddit；留空为全局搜索")
    parser.add_argument("-k", "--keyword", required=True, help="关键词或搜索表达式")
    parser.add_argument("--limit", type=int, default=0, help="需要保存的帖子数量；0 表示无限")
    parser.add_argument("--output", type=Path, help="输出目录；默认按 subreddit 和关键词生成")
    parser.add_argument("--after", help="起始日期时间，例如 2024-01-01 08:30")
    parser.add_argument("--before", help="结束日期时间，例如 2025-01-01 18:00")
    parser.add_argument("--title-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--comment-limit",
        type=int,
        default=1000,
        help="每帖最低评论请求容量或固定上限，默认 1000",
    )
    parser.add_argument(
        "--fixed-comment-limit",
        action="store_true",
        help="严格使用 --comment-limit；默认会按 Reddit 评论计数提高上限以尽量抓全",
    )
    parser.add_argument("--comment-depth", type=int, default=20, help="评论递归深度，默认 20")
    parser.add_argument("--no-comments", action="store_true", help="只采集帖子，不采集评论")
    parser.add_argument("--max-pages", type=int, default=0, help="最多扫描的搜索页数；0 表示无限")
    parser.add_argument(
        "--sort",
        choices=("relevance", "new", "comments", "top", "hot"),
        default="relevance",
        help="搜索排序：关联性、最新、评论最多、最高得分或热门；默认关联性",
    )
    parser.add_argument("--delay", type=float, default=0.75, help="搜索分页间隔秒数")
    parser.add_argument(
        "--use-saved-cookie",
        action="store_true",
        help="使用界面预登录后保存的 Reddit Cookie",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.platform.startswith("win"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    args = build_parser().parse_args(argv)
    config: CollectorConfig | None = None
    collector: KeywordCollector | None = None
    try:
        config = CollectorConfig(
            subreddit=args.subreddit,
            keyword=args.keyword,
            post_limit=args.limit,
            output_dir=args.output,
            after=args.after,
            before=args.before,
            collect_comments=not args.no_comments,
            all_comments=not args.fixed_comment_limit,
            minimum_comment_capacity=args.comment_limit,
            max_comment_depth=args.comment_depth,
            max_pages=args.max_pages,
            search_sort=args.sort,
            request_delay=args.delay,
            use_saved_reddit_login=args.use_saved_cookie,
        )
        collector = KeywordCollector(config)
        summary = collector.run()
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print("\n用户已停止采集。", file=sys.stderr)
        return 130
    except requests.HTTPError as exc:
        if config is not None:
            report_http_error(
                exc.response,
                config.output_dir,
                collector.session if collector is not None else None,
            )
        else:
            print(f"采集失败：{exc}", file=sys.stderr)
        return 1
    except (ValueError, requests.RequestException) as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
