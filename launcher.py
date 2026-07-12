"""Native Windows GUI launcher for Reddit King.

In a source checkout collector tasks run through uv.  In a PyInstaller build,
the executable starts itself in worker mode, so end users do not need Python,
uv, or the source files.  Collector output is written directly to run.log;
the on-screen view keeps only a recent window of that complete file.
"""

from __future__ import annotations

import codecs
import datetime as dt
import os
import re
import signal
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path
from tkinter import (
    BooleanVar,
    IntVar,
    StringVar,
    TclError,
    Tk,
    Toplevel,
    filedialog,
    messagebox,
    scrolledtext,
    ttk,
)

from scraper.reddit_auth import capture_and_save_reddit_login, has_saved_reddit_login

FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
if FROZEN and sys.platform == "darwin":
    APP_DATA_ROOT = Path.home() / "Library" / "Application Support" / "Reddit-King"
    OUTPUT_ROOT = Path.home() / "Documents" / "Reddit-King" / "output"
else:
    APP_DATA_ROOT = ROOT
    OUTPUT_ROOT = ROOT / "output"
URL_PATTERN = re.compile(r"https?://[^\s|]+")
SORT_VALUES = {
    "关联性": "relevance",
    "最新": "new",
    "评论最多": "comments",
    "最高得分": "top",
    "热门": "hot",
}
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def safe_slug(value: str, fallback: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_-]+", "_", value.strip()).strip("_")
    return (slug[:60] or fallback)


class RedditKingGui:
    MAX_VISIBLE_LOG_CHARS = 600_000
    KEEP_VISIBLE_LOG_CHARS = 450_000

    def __init__(self, root: Tk) -> None:
        self.root = root
        self.process: subprocess.Popen[bytes] | None = None
        self.last_output_path: Path | None = None
        self.run_log_path: Path | None = None
        self.log_offset = 0
        self.log_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.url_tag_counter = 0
        self.stop_requested = False

        root.title("Reddit King - 关键词帖子与评论采集器")
        window_width = 820
        window_height = 720
        window_x = max(0, (root.winfo_screenwidth() - window_width) // 2)
        window_y = max(0, (root.winfo_screenheight() - window_height) // 2)
        root.geometry(f"{window_width}x{window_height}+{window_x}+{window_y}")
        root.minsize(820, 720)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.subreddit = StringVar()
        self.keyword = StringVar()
        self.sort_label = StringVar(value="关联性")
        self.post_limit = IntVar(value=100)
        self.unlimited_posts = BooleanVar(value=True)
        self.after = StringVar()
        self.before = StringVar()
        self.comment_depth = IntVar(value=20)
        self.max_pages = IntVar(value=0)
        self.no_comments = BooleanVar(value=False)
        self.use_saved_cookie = BooleanVar(value=False)
        self.output_root = StringVar(value=str(OUTPUT_ROOT))
        self.status = StringVar(value="就绪")
        self.login_status = StringVar(
            value="已保存，可按需启用" if has_saved_reddit_login() else "尚未预登录"
        )
        self.login_thread: threading.Thread | None = None

        self._build_ui()
        self.unlimited_posts.trace_add("write", self._toggle_post_limit)
        self._toggle_post_limit()
        self.root.after(500, self.poll)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.columnconfigure(3, weight=1)
        outer.rowconfigure(7, weight=1)

        title = ttk.Label(outer, text="Reddit King 关键词采集器", font=("Microsoft YaHei UI", 16, "bold"))
        title.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 16))
        ttk.Label(outer, text="搜索排序").grid(row=0, column=2, sticky="e", padx=(8, 6))
        sort_box = ttk.Combobox(
            outer,
            textvariable=self.sort_label,
            values=tuple(SORT_VALUES),
            state="readonly",
            width=15,
        )
        sort_box.grid(row=0, column=3, sticky="e", pady=(0, 16))

        ttk.Label(outer, text="Subreddit（可留空）").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(outer, textvariable=self.subreddit).grid(row=1, column=1, sticky="ew", padx=(8, 20), pady=5)
        ttk.Label(outer, text="关键词/表达式").grid(row=1, column=2, sticky="w", pady=5)
        ttk.Entry(outer, textvariable=self.keyword).grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=5)

        ttk.Label(outer, text="帖子数量").grid(row=2, column=0, sticky="w", pady=5)
        post_frame = ttk.Frame(outer)
        post_frame.grid(row=2, column=1, sticky="ew", padx=(8, 20), pady=5)
        self.post_limit_box = ttk.Spinbox(post_frame, from_=1, to=100000, textvariable=self.post_limit, width=12)
        self.post_limit_box.pack(side="left")
        ttk.Checkbutton(post_frame, text="无限", variable=self.unlimited_posts).pack(side="left", padx=10)
        ttk.Checkbutton(
            outer,
            text="不采集评论（默认尽量抓全）",
            variable=self.no_comments,
        ).grid(
            row=2, column=2, columnspan=2, sticky="w", padx=(8, 0), pady=5
        )

        ttk.Label(outer, text="起始日期时间").grid(row=3, column=0, sticky="w", pady=5)
        after_frame = ttk.Frame(outer)
        after_frame.grid(row=3, column=1, sticky="ew", padx=(8, 20), pady=5)
        after_frame.columnconfigure(0, weight=1)
        ttk.Entry(after_frame, textvariable=self.after).grid(row=0, column=0, sticky="ew")
        ttk.Button(after_frame, text="选择...", command=self.choose_after_datetime).grid(
            row=0, column=1, padx=(6, 0)
        )
        ttk.Label(outer, text="结束日期时间").grid(row=3, column=2, sticky="w", pady=5)
        before_frame = ttk.Frame(outer)
        before_frame.grid(row=3, column=3, sticky="ew", padx=(8, 0), pady=5)
        before_frame.columnconfigure(0, weight=1)
        ttk.Entry(before_frame, textvariable=self.before).grid(row=0, column=0, sticky="ew")
        ttk.Button(before_frame, text="选择...", command=self.choose_before_datetime).grid(
            row=0, column=1, padx=(6, 0)
        )

        ttk.Label(outer, text="评论深度").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Spinbox(outer, from_=0, to=100, textvariable=self.comment_depth, width=14).grid(
            row=4, column=1, sticky="w", padx=(8, 20), pady=5
        )
        ttk.Label(outer, text="最大搜索页（0=无限）").grid(row=4, column=2, sticky="w", pady=5)
        ttk.Spinbox(outer, from_=0, to=10000, textvariable=self.max_pages, width=14).grid(
            row=4, column=3, sticky="w", padx=(8, 0), pady=5
        )

        output_frame = ttk.Frame(outer)
        output_frame.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(12, 10))
        output_frame.columnconfigure(1, weight=1)
        ttk.Label(output_frame, text="输出根目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(output_frame, textvariable=self.output_root).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(output_frame, text="选择...", command=self.choose_output).grid(row=0, column=2)
        ttk.Label(output_frame, text="Reddit 登录").grid(row=1, column=0, sticky="w", pady=(10, 0))
        login_controls = ttk.Frame(output_frame)
        login_controls.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(10, 0))
        self.login_button = ttk.Button(
            login_controls,
            text="预登录 / 更新 Cookie",
            command=self.start_reddit_login,
        )
        self.login_button.pack(side="left")
        ttk.Checkbutton(
            login_controls,
            text="本次使用已保存 Cookie",
            variable=self.use_saved_cookie,
            command=self.update_login_status,
        ).pack(side="left", padx=(12, 8))
        ttk.Label(login_controls, textvariable=self.login_status).pack(side="left")

        action_frame = ttk.Frame(outer)
        action_frame.grid(row=6, column=0, columnspan=4, sticky="new", pady=(0, 8))
        self.start_button = ttk.Button(action_frame, text="开始采集", command=self.start_collection)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(action_frame, text="停止", command=self.stop_collection, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(action_frame, text="打开输出目录", command=self.open_output).pack(side="left")
        ttk.Label(action_frame, textvariable=self.status).pack(side="right")

        log_frame = ttk.Frame(outer)
        log_frame.grid(row=7, column=0, columnspan=4, sticky="nsew")
        self.log = scrolledtext.ScrolledText(
            log_frame,
            wrap="word",
            state="disabled",
            background="#1c1c1c",
            foreground="#dcdcdc",
            insertbackground="white",
            font=("Consolas", 9),
        )
        self.log.pack(fill="both", expand=True)

        ttk.Label(
            outer,
            text="© 2026 Reddit King · 开发者：小杨",
            anchor="center",
            foreground="#666666",
        ).grid(row=8, column=0, columnspan=4, sticky="ew", pady=(10, 0))

    def _toggle_post_limit(self, *_args: object) -> None:
        self.post_limit_box.configure(state="disabled" if self.unlimited_posts.get() else "normal")

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(title="选择采集结果输出根目录")
        if selected:
            self.output_root.set(selected)

    def update_login_status(self) -> None:
        if not has_saved_reddit_login():
            self.login_status.set("尚未预登录")
        elif self.use_saved_cookie.get():
            self.login_status.set("已保存，本次启用")
        else:
            self.login_status.set("已保存，本次不使用")

    def start_reddit_login(self) -> None:
        if self.login_thread and self.login_thread.is_alive():
            return
        self.login_button.configure(state="disabled")
        self.login_status.set("请在弹出的 Chrome 中完成登录…")
        self.append_log("正在打开独立的 Reddit 登录窗口；登录成功后会自动保存 Cookie。\n")

        def login_worker() -> None:
            try:
                count = capture_and_save_reddit_login()
            except Exception as exc:
                message = str(exc)
                self.root.after(0, lambda: self.finish_reddit_login(error=message))
            else:
                self.root.after(0, lambda: self.finish_reddit_login(cookie_count=count))

        self.login_thread = threading.Thread(target=login_worker, daemon=True)
        self.login_thread.start()

    def finish_reddit_login(
        self, *, cookie_count: int = 0, error: str | None = None
    ) -> None:
        self.login_button.configure(state="normal")
        if error:
            self.login_status.set("登录未完成；原有 Cookie 保持不变")
            self.append_log(f"Reddit 预登录未完成：{error}\n")
            return
        self.use_saved_cookie.set(True)
        self.login_status.set("已保存，本次启用")
        self.append_log(f"Reddit 预登录成功：已安全保存 {cookie_count} 项 Cookie。\n")

    def choose_after_datetime(self) -> None:
        self.choose_datetime(self.after, "选择起始日期时间")

    def choose_before_datetime(self) -> None:
        self.choose_datetime(self.before, "选择结束日期时间")

    def choose_datetime(self, target: StringVar, title: str) -> None:
        """Open a dependency-free date/time picker for a search bound."""
        initial = dt.datetime.now().replace(second=0, microsecond=0)
        raw_value = target.get().strip()
        if raw_value:
            try:
                parsed = dt.datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
                initial = parsed.replace(tzinfo=None, second=0, microsecond=0)
            except ValueError:
                pass

        dialog = Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.grab_set()

        panel = ttk.Frame(dialog, padding=16)
        panel.pack(fill="both", expand=True)

        values = {
            "年": (IntVar(value=initial.year), 1970, 2100, 6),
            "月": (IntVar(value=initial.month), 1, 12, 4),
            "日": (IntVar(value=initial.day), 1, 31, 4),
            "时": (IntVar(value=initial.hour), 0, 23, 4),
            "分": (IntVar(value=initial.minute), 0, 59, 4),
        }
        for column, (label, (variable, minimum, maximum, width)) in enumerate(values.items()):
            ttk.Label(panel, text=label).grid(row=0, column=column, padx=4, pady=(0, 4))
            ttk.Spinbox(
                panel,
                from_=minimum,
                to=maximum,
                textvariable=variable,
                width=width,
                justify="center",
            ).grid(row=1, column=column, padx=4)

        hint = ttk.Label(panel, text="保存格式：YYYY-MM-DD HH:MM")
        hint.grid(row=2, column=0, columnspan=5, sticky="w", pady=(12, 8))

        def apply_value() -> None:
            try:
                selected = dt.datetime(
                    values["年"][0].get(),
                    values["月"][0].get(),
                    values["日"][0].get(),
                    values["时"][0].get(),
                    values["分"][0].get(),
                )
            except (TclError, ValueError, TypeError):
                messagebox.showerror("日期无效", "请选择有效的日期和时间。", parent=dialog)
                return
            target.set(selected.strftime("%Y-%m-%d %H:%M"))
            dialog.destroy()

        def use_now() -> None:
            target.set(dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
            dialog.destroy()

        def use_today_start() -> None:
            target.set(dt.datetime.now().strftime("%Y-%m-%d 00:00"))
            dialog.destroy()

        def use_today_end() -> None:
            target.set(dt.datetime.now().strftime("%Y-%m-%d 23:59"))
            dialog.destroy()

        def clear_value() -> None:
            target.set("")
            dialog.destroy()

        buttons = ttk.Frame(panel)
        buttons.grid(row=3, column=0, columnspan=5, sticky="e")
        ttk.Button(buttons, text="清空", command=clear_value).pack(side="left")
        ttk.Button(buttons, text="今天 00:00", command=use_today_start).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="今天 23:59", command=use_today_end).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(buttons, text="现在", command=use_now).pack(side="left", padx=6)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left")
        ttk.Button(buttons, text="确定", command=apply_value).pack(side="left", padx=(6, 0))

        dialog.bind("<Return>", lambda _event: apply_value())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.update_idletasks()
        width = dialog.winfo_reqwidth()
        height = dialog.winfo_reqheight()
        x = max(0, (dialog.winfo_screenwidth() - width) // 2)
        y = max(0, (dialog.winfo_screenheight() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.wait_visibility()
        dialog.focus_set()

    def new_output_path(self) -> Path:
        base = Path(self.output_root.get().strip() or OUTPUT_ROOT).expanduser().resolve()
        subreddit = safe_slug(self.subreddit.get().removeprefix("r/"), "all")
        keyword = safe_slug(self.keyword.get(), "keyword")
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = base / f"{stamp}_{subreddit}_{keyword}"
        suffix = 1
        while candidate.exists():
            candidate = base / f"{stamp}_{subreddit}_{keyword}_{suffix}"
            suffix += 1
        return candidate

    def build_command(self, output_path: Path) -> list[str]:
        limit = 0 if self.unlimited_posts.get() else self.post_limit.get()
        if FROZEN:
            command = [
                sys.executable,
                "--collector-worker",
                "--worker-log",
                str(self.run_log_path),
            ]
        else:
            command = ["uv", "run", "--frozen", "python", "-u", "main.py"]
        command.extend(
            [
            "--keyword",
            self.keyword.get().strip(),
            "--sort",
            SORT_VALUES[self.sort_label.get()],
            "--limit",
            str(limit),
            "--comment-depth",
            str(self.comment_depth.get()),
            "--max-pages",
            str(self.max_pages.get()),
            "--output",
            str(output_path),
            ]
        )
        subreddit = self.subreddit.get().strip()
        if subreddit:
            command.extend(("--subreddit", subreddit))
        if self.after.get().strip():
            command.extend(("--after", self.after.get().strip()))
        if self.before.get().strip():
            command.extend(("--before", self.before.get().strip()))
        if self.no_comments.get():
            command.append("--no-comments")
        if self.use_saved_cookie.get():
            command.append("--use-saved-cookie")
        return command

    def start_collection(self) -> None:
        if self.process and self.process.poll() is None:
            return
        if not self.keyword.get().strip():
            messagebox.showerror("输入错误", "关键词不能为空；Subreddit 可以留空。")
            return
        try:
            if not self.unlimited_posts.get() and self.post_limit.get() < 1:
                raise ValueError("帖子数量必须大于 0")
            if self.comment_depth.get() < 0 or self.max_pages.get() < 0:
                raise ValueError("评论深度和最大页数不能为负数")
            output_path = self.new_output_path()
        except (OSError, ValueError) as exc:
            messagebox.showerror("输入错误", str(exc))
            return

        output_path.mkdir(parents=True, exist_ok=True)
        APP_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        self.last_output_path = output_path
        self.run_log_path = output_path / "run.log"
        self.log_offset = 0
        self.log_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.clear_log()

        scope = f"r/{self.subreddit.get().strip()}" if self.subreddit.get().strip() else "全 Reddit"
        self.append_log(f"启动采集：{scope} | {self.keyword.get().strip()}\n")
        self.append_log(f"运行日志：{self.run_log_path}\n")
        self.append_log("显示策略：界面仅保留最近日志，run.log 和 CSV 始终完整落盘\n")

        command = self.build_command(output_path)
        env = os.environ.copy()
        env.update(
            {
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                "UV_CACHE_DIR": str(APP_DATA_ROOT / ".uv-cache"),
            }
        )
        try:
            with self.run_log_path.open("wb", buffering=0) as log_handle:
                log_handle.write(
                    f"[{dt.datetime.now():%H:%M:%S}] Runner started\r\n".encode("utf-8")
                )
                self.process = subprocess.Popen(
                    command,
                    cwd=APP_DATA_ROOT,
                    env=env,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    creationflags=CREATE_NO_WINDOW,
                    start_new_session=os.name != "nt",
                )
        except (OSError, subprocess.SubprocessError) as exc:
            self.append_log(f"启动失败：{exc}\n")
            self.status.set("启动失败")
            self.process = None
            return

        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.stop_requested = False
        self.status.set(f"运行中，PID {self.process.pid}")

    def poll(self) -> None:
        self.read_new_log()
        if self.process and self.process.poll() is not None:
            exit_code = self.process.returncode
            self.append_runner_exit(exit_code)
            self.read_new_log()
            if self.stop_requested:
                self.status.set("采集已停止")
            else:
                self.status.set("采集完成" if exit_code == 0 else f"任务结束，代码 {exit_code}")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.process = None
            self.stop_requested = False
        self.root.after(500, self.poll)

    def read_new_log(self) -> None:
        if not self.run_log_path or not self.run_log_path.exists():
            return
        try:
            size = self.run_log_path.stat().st_size
            if size < self.log_offset:
                self.log_offset = 0
                self.log_decoder = codecs.getincrementaldecoder("utf-8")("replace")
            if size == self.log_offset:
                return
            with self.run_log_path.open("rb") as handle:
                handle.seek(self.log_offset)
                data = handle.read()
            self.log_offset += len(data)
            text = self.log_decoder.decode(data)
            if text:
                self.append_log(text.lstrip("\ufeff") if self.log_offset == len(data) else text)
        except OSError:
            return

    def append_runner_exit(self, exit_code: int) -> None:
        if not self.run_log_path:
            return
        try:
            with self.run_log_path.open("ab") as handle:
                handle.write(
                    f"[{dt.datetime.now():%H:%M:%S}] Runner exited | code {exit_code}\r\n".encode(
                        "utf-8"
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            pass

    def append_log(self, text: str) -> None:
        if not text:
            return
        self.log.configure(state="normal")
        base_index = self.log.index("end-1c")
        self.log.insert("end", text)
        for match in URL_PATTERN.finditer(text):
            url = match.group(0).rstrip(".,;)")
            if not url:
                continue
            tag = f"url_{self.url_tag_counter}"
            self.url_tag_counter += 1
            start = f"{base_index}+{match.start()}c"
            end = f"{start}+{len(url)}c"
            self.log.tag_add(tag, start, end)
            self.log.tag_config(tag, foreground="#4da3ff", underline=True)
            self.log.tag_bind(tag, "<Button-1>", lambda _event, target=url: webbrowser.open(target))
            self.log.tag_bind(tag, "<Enter>", lambda _event: self.log.configure(cursor="hand2"))
            self.log.tag_bind(tag, "<Leave>", lambda _event: self.log.configure(cursor=""))
        self.trim_visible_log()
        self.log.see("end")
        self.log.configure(state="disabled")

    def trim_visible_log(self) -> None:
        char_count = len(self.log.get("1.0", "end-1c"))
        if char_count <= self.MAX_VISIBLE_LOG_CHARS:
            return
        remove_count = char_count - self.KEEP_VISIBLE_LOG_CHARS
        cut = self.log.index(f"1.0+{remove_count}c lineend +1c")
        self.log.delete("1.0", cut)

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def stop_collection(self) -> None:
        if not self.process or self.process.poll() is not None:
            return
        self.append_log("正在停止采集进程...\n")
        process = self.process
        self.stop_requested = True
        self.stop_button.configure(state="disabled")
        self.status.set("正在停止...")
        self._signal_process_tree(process, force=False)
        if os.name != "nt":
            self.root.after(3000, lambda: self._force_stop_if_running(process))

    def _signal_process_tree(
        self, process: subprocess.Popen[bytes], *, force: bool
    ) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
            return
        try:
            os.killpg(
                os.getpgid(process.pid),
                signal.SIGKILL if force else signal.SIGINT,
            )
        except (OSError, ProcessLookupError):
            if force and process.poll() is None:
                process.kill()

    def _force_stop_if_running(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            self.append_log("采集进程未及时退出，正在强制停止...\n")
            self._signal_process_tree(process, force=True)

    def open_output(self) -> None:
        target = (
            self.last_output_path
            or Path(self.output_root.get().strip() or OUTPUT_ROOT).expanduser().resolve()
        )
        try:
            target.mkdir(parents=True, exist_ok=True)
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            elif os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except (OSError, subprocess.SubprocessError) as exc:
            messagebox.showerror("打开失败", f"无法打开输出目录：\n{target}\n\n{exc}")

    def on_close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("确认关闭", "采集仍在运行，关闭窗口会停止任务。是否继续？"):
                return
            process = self.process
            self._signal_process_tree(process, force=False)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._signal_process_tree(process, force=True)
        self.root.destroy()


def run_collector_worker() -> int:
    """Run the CLI inside a frozen GUI executable and preserve its log output."""
    args = sys.argv[1:]
    args.remove("--collector-worker")

    worker_log: Path | None = None
    if "--worker-log" in args:
        index = args.index("--worker-log")
        try:
            worker_log = Path(args[index + 1])
        except IndexError as exc:
            raise SystemExit("--worker-log requires a path") from exc
        del args[index : index + 2]

    if worker_log is not None:
        worker_log.parent.mkdir(parents=True, exist_ok=True)
        stream = worker_log.open("a", encoding="utf-8", buffering=1)
        sys.stdout = stream
        sys.stderr = stream
    else:
        # A --windowed PyInstaller executable has no console streams.
        if sys.stdout is None:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
        if sys.stderr is None:
            sys.stderr = sys.stdout

    from main import main as collector_main

    sys.argv = [sys.argv[0], *args]
    return collector_main()


def main() -> int:
    if "--collector-worker" in sys.argv:
        return run_collector_worker()

    root = Tk()
    app = RedditKingGui(root)
    if "--validate-only" in sys.argv:
        root.withdraw()
        app.append_log("x" * (app.MAX_VISIBLE_LOG_CHARS + 1000))
        remaining = len(app.log.get("1.0", "end-1c"))
        if remaining > app.MAX_VISIBLE_LOG_CHARS:
            raise RuntimeError("visible log retention validation failed")
        root.destroy()
        if sys.stdout is not None:
            print("Python GUI construction and visible-log retention OK")
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except Exception:
        details = traceback.format_exc()
        (ROOT / "launcher-error.log").write_text(details, encoding="utf-8-sig")
        try:
            messagebox.showerror("Reddit King 启动失败", details)
        except Exception:
            pass
        raise
    raise SystemExit(exit_code)
