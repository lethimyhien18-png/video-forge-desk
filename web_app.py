#!/usr/bin/env python3
"""
Local web UI for the video download + edit agent.
"""

from __future__ import annotations

import html
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional


ROOT_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = ROOT_DIR / "downloads"
SCRIPT_PATH = ROOT_DIR / "video_agent.py"
HOST = "0.0.0.0"
PORT = 8765
FALLBACK_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


@dataclass
class Job:
    id: str
    title: str
    command: List[str]
    status: str = "queued"
    return_code: Optional[int] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    log_lines: List[str] = field(default_factory=list)
    output_path: str = ""
    cleanup_file: str = ""
    downloadable_path: str = ""
    downloadable_url: str = ""

    def append_log(self, text: str) -> None:
        if not text:
            return
        self.log_lines.extend(text.splitlines())
        self.log_lines = self.log_lines[-300:]

    def to_dict(self) -> Dict[str, object]:
        payload = asdict(self)
        payload["created_label"] = time.strftime("%H:%M:%S", time.localtime(self.created_at))
        return payload


jobs: Dict[str, Job] = {}
jobs_lock = threading.Lock()


def dependency_report() -> Dict[str, bool]:
    search_path = os.environ.get("PATH", "")
    for bin_dir in FALLBACK_BIN_DIRS:
        if bin_dir not in search_path.split(os.pathsep):
            search_path = f"{bin_dir}{os.pathsep}{search_path}" if search_path else bin_dir
    return {
        "yt_dlp": bool(shutil.which("yt-dlp", path=search_path)),
        "ffmpeg": bool(shutil.which("ffmpeg", path=search_path)),
        "brew": bool(shutil.which("brew", path=search_path)),
    }


def build_runtime_env() -> Dict[str, str]:
    env = os.environ.copy()
    current_path = env.get("PATH", "")
    for bin_dir in reversed(FALLBACK_BIN_DIRS):
        if bin_dir not in current_path.split(os.pathsep):
            current_path = f"{bin_dir}{os.pathsep}{current_path}" if current_path else bin_dir
    env["PATH"] = current_path
    return env


def detect_downloadable_file(path_hint: str) -> Optional[Path]:
    if not path_hint:
        return None
    path = Path(path_hint)
    if path.is_file():
        return path
    if not path.exists() or not path.is_dir():
        return None
    candidates = [
        item for item in path.rglob("*")
        if item.is_file() and item.suffix.lower() not in {".json", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt", ".txt"}
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def list_recent_downloads(limit: int = 10) -> List[Dict[str, str]]:
    if not DOWNLOADS_DIR.exists():
        return []
    files = [
        item for item in DOWNLOADS_DIR.rglob("*")
        if item.is_file() and item.suffix.lower() not in {".json", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt", ".txt"}
    ]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    results: List[Dict[str, str]] = []
    for item in files[:limit]:
        try:
            relative = item.resolve().relative_to(ROOT_DIR.resolve())
            url = "/" + str(relative).replace(os.sep, "/")
        except ValueError:
            continue
        results.append(
            {
                "name": item.name,
                "path": str(item),
                "url": url,
                "size": f"{item.stat().st_size / 1024:.1f} KB",
            }
        )
    return results


def bool_from_form(value: Optional[str]) -> bool:
    return value in {"1", "true", "on", "yes"}


def pick_port(env_value: Optional[str]) -> int:
    if not env_value:
        return PORT
    try:
        return int(env_value)
    except ValueError:
        return PORT


def get_listen_port() -> int:
    return pick_port(os.environ.get("PORT") or os.environ.get("VIDEO_AGENT_WEB_PORT"))


def detect_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def make_job(title: str, command: List[str], output_path: str = "", cleanup_file: str = "") -> Job:
    job = Job(
        id=uuid.uuid4().hex[:10],
        title=title,
        command=command,
        output_path=output_path,
        cleanup_file=cleanup_file,
    )
    with jobs_lock:
        jobs[job.id] = job
    thread = threading.Thread(target=run_job, args=(job.id,), daemon=True)
    thread.start()
    return job


def run_job(job_id: str) -> None:
    with jobs_lock:
        job = jobs[job_id]
        job.status = "running"
        job.started_at = time.time()

    process = subprocess.Popen(
        job.command,
        cwd=str(ROOT_DIR),
        env=build_runtime_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    try:
        assert process.stdout is not None
        for line in process.stdout:
            with jobs_lock:
                jobs[job_id].append_log(line.rstrip())
        return_code = process.wait()
    finally:
        if job.cleanup_file:
            try:
                Path(job.cleanup_file).unlink(missing_ok=True)
            except OSError:
                pass

    with jobs_lock:
        latest = jobs[job_id]
        latest.return_code = return_code
        latest.finished_at = time.time()
        latest.status = "done" if return_code == 0 else "failed"
        downloadable = detect_downloadable_file(latest.output_path)
        if downloadable:
            latest.downloadable_path = str(downloadable)
            try:
                relative = downloadable.resolve().relative_to(ROOT_DIR.resolve())
                latest.downloadable_url = "/" + str(relative).replace(os.sep, "/")
            except ValueError:
                latest.downloadable_url = ""


def build_common_download_args(form: Dict[str, str]) -> List[str]:
    args = [
        "--output-dir",
        form.get("output_dir", "downloads") or "downloads",
        "--filename",
        form.get("filename") or "%(uploader|unknown)s/%(upload_date>%Y-%m-%d)s_%(title).180B_[%(id)s].%(ext)s",
        "--quality",
        form.get("quality", "best") or "best",
        "--remux-video",
        form.get("remux_video", "mp4") or "mp4",
    ]
    cookies = form.get("cookies", "").strip()
    if cookies:
        args.extend(["--cookies", cookies])
    if bool_from_form(form.get("audio_only")):
        args.append("--audio-only")
    if bool_from_form(form.get("subs")):
        args.append("--subs")
    if bool_from_form(form.get("write_thumbnail")):
        args.append("--write-thumbnail")
    if bool_from_form(form.get("write_info_json")):
        args.append("--write-info-json")
    if bool_from_form(form.get("no_archive")):
        args.append("--no-archive")
    return args


def build_edit_args(form: Dict[str, str]) -> List[str]:
    args: List[str] = []
    for key in ("start", "end", "duration", "crop", "resize"):
        value = form.get(key, "").strip()
        if value:
            args.extend([f"--{key}", value])
    preset_name = form.get("preset_name", "none").strip() or "none"
    args.extend(["--preset-name", preset_name])
    if bool_from_form(form.get("mute")):
        args.append("--mute")
    if bool_from_form(form.get("extract_audio")):
        args.append("--extract-audio")
    video_codec = form.get("video_codec", "libx264").strip() or "libx264"
    encode_preset = form.get("encode_preset", "slow").strip() or "slow"
    crf = form.get("crf", "18").strip() or "18"
    args.extend(["--video-codec", video_codec, "--crf", crf, "--encode-preset", encode_preset])
    output = form.get("output", "").strip()
    if output:
        args.extend(["--output", output])
    return args


def create_download_job(form: Dict[str, str]) -> Job:
    url = form.get("url", "").strip()
    mode = form.get("mode", "download").strip() or "download"
    command = ["python3", str(SCRIPT_PATH), mode, url]
    command.extend(build_common_download_args(form))
    if mode == "workflow":
        command.extend(build_edit_args(form))
    title = f"{mode}: {url[:72]}"
    output_path = str(ROOT_DIR / (form.get("output_dir", "downloads") or "downloads"))
    return make_job(title, command, output_path=output_path)


def create_batch_job(form: Dict[str, str]) -> Job:
    urls_raw = form.get("urls", "").strip()
    if not urls_raw:
        raise ValueError("Danh sach URL dang trong.")
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", dir=str(ROOT_DIR), encoding="utf-8") as handle:
        handle.write(urls_raw)
        temp_path = handle.name
    command = ["python3", str(SCRIPT_PATH), "batch", temp_path]
    command.extend(build_common_download_args(form))
    title = f"batch: {len([line for line in urls_raw.splitlines() if line.strip()])} urls"
    output_path = str(ROOT_DIR / (form.get("output_dir", "downloads") or "downloads"))
    return make_job(title, command, output_path=output_path, cleanup_file=temp_path)


def create_edit_job(form: Dict[str, str]) -> Job:
    input_file = form.get("input_file", "").strip()
    if not input_file:
        raise ValueError("Chua nhap duong dan file can edit.")
    command = ["python3", str(SCRIPT_PATH), "edit", input_file]
    command.extend(build_edit_args(form))
    title = f"edit: {Path(input_file).name}"
    output_path = form.get("output", "").strip() or str(Path(input_file).with_name(f"{Path(input_file).stem}_edited{Path(input_file).suffix}"))
    return make_job(title, command, output_path=output_path)


def render_page(error_message: str = "") -> str:
    with jobs_lock:
        job_list = sorted(jobs.values(), key=lambda item: item.created_at, reverse=True)
        job_rows = [job.to_dict() for job in job_list[:12]]

    deps = dependency_report()
    recent_files = list_recent_downloads()
    jobs_json = json.dumps(job_rows, ensure_ascii=False)
    recent_files_json = json.dumps(recent_files, ensure_ascii=False)
    error_html = ""
    if error_message:
        error_html = f'<div class="alert">{html.escape(error_message)}</div>'
    missing_tools = []
    if not deps["yt_dlp"]:
        missing_tools.append("yt-dlp")
    if not deps["ffmpeg"]:
        missing_tools.append("ffmpeg")
    dependency_html = ""
    if missing_tools:
        install_hint = "Chạy ./setup_deps.sh để xem cách cài nhanh."
        if os.uname().sysname == "Darwin" and not deps["brew"]:
            install_hint = 'Máy macOS này chưa có Homebrew. Hãy cài Homebrew trước, rồi chạy `brew install yt-dlp ffmpeg` hoặc `./setup_deps.sh`.'
        dependency_html = (
            '<div class="alert"><strong>Chưa thể tải video thật.</strong> '
            f'Máy này đang thiếu: {html.escape(", ".join(missing_tools))}. '
            f'{html.escape(install_hint)}</div>'
        )

    return f"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Video Forge Desk</title>
  <style>
    :root {{
      --bg: #efe8db;
      --bg-2: #ead8c0;
      --ink: #171411;
      --muted: rgba(23, 20, 17, 0.60);
      --line: rgba(23, 20, 17, 0.11);
      --accent: #d06d2d;
      --accent-soft: rgba(208, 109, 45, 0.12);
      --card: rgba(255, 250, 244, 0.74);
      --radius: 36px;
      --shadow: 0 22px 56px rgba(85, 57, 28, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 0% 0%, rgba(208, 109, 45, 0.16), transparent 24%),
        radial-gradient(circle at 100% 0%, rgba(210, 173, 124, 0.16), transparent 24%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg-2) 100%);
      padding: 28px;
    }}
    .shell {{
      width: min(1280px, 100%);
      margin: 0 auto;
    }}
    .alert {{
      margin-bottom: 18px;
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(178,34,34,0.08);
      border: 1px solid rgba(178,34,34,0.15);
      color: #8d1c1c;
    }}
    .job-banner {{
      display: none;
      position: sticky;
      top: 14px;
      z-index: 10;
      margin-bottom: 18px;
      padding: 16px 18px;
      border-radius: 20px;
      font-size: 16px;
      font-weight: 800;
      line-height: 1.5;
      box-shadow: 0 18px 36px rgba(85, 57, 28, 0.12);
    }}
    .job-banner.show {{
      display: block;
    }}
    .job-banner.done {{
      background: #fff4e8;
      border: 1px solid rgba(208, 109, 45, 0.24);
      color: #8f4b1c;
    }}
    .job-banner.running {{
      background: #fff6da;
      border: 1px solid rgba(255,186,73,0.30);
      color: #7d4a00;
    }}
    .job-banner.failed {{
      background: rgba(178,34,34,0.08);
      border: 1px solid rgba(178,34,34,0.18);
      color: #8d1c1c;
    }}
    .status-panel {{
      margin-top: 8px;
      padding-top: 8px;
      display: grid;
      gap: 14px;
    }}
    .status-panel h2 {{
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      font-size: clamp(30px, 3vw, 40px);
    }}
    .status-empty {{
      color: var(--muted);
      font-size: clamp(18px, 2vw, 22px);
    }}
    .status-item {{
      border: 1px solid rgba(23, 20, 17, 0.10);
      border-radius: 24px;
      background: rgba(255,255,255,0.74);
      overflow: hidden;
    }}
    .status-head {{
      padding: 14px 16px;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
    }}
    .status-title {{
      font-size: 18px;
      font-weight: 800;
    }}
    .status-meta {{
      font-size: 14px;
      color: var(--muted);
    }}
    .status-badge {{
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}
    .queued {{ background: rgba(33,66,77,0.10); color: #21424d; }}
    .running {{ background: rgba(255,186,73,0.22); color: #7d4a00; }}
    .done {{ background: rgba(76,175,80,0.18); color: #1f6a2b; }}
    .failed {{ background: rgba(178,34,34,0.12); color: #8d1c1c; }}
    .status-body {{
      padding: 0 16px 16px;
      display: grid;
      gap: 10px;
    }}
    .status-path {{
      font-size: 15px;
      color: var(--muted);
      word-break: break-word;
    }}
    .status-log {{
      margin: 0;
      padding: 12px;
      border-radius: 14px;
      background: rgba(22,18,13,0.92);
      color: #f6e8cf;
      max-height: 170px;
      overflow: auto;
      font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
      white-space: pre-wrap;
    }}
    .download-link {{
      display: inline-flex;
      width: fit-content;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(208, 109, 45, 0.12);
      color: #9a4f1f;
      text-decoration: none;
      font-weight: 800;
    }}
    .download-link.primary {{
      padding: 14px 18px;
      background: linear-gradient(135deg, #cf6a2f, #d97b22);
      color: #fffaf2;
      font-size: 16px;
      box-shadow: 0 10px 24px rgba(208, 109, 45, 0.18);
    }}
    .status-note {{
      padding: 12px 14px;
      border-radius: 16px;
      font-size: 15px;
      font-weight: 700;
      line-height: 1.5;
    }}
    .status-note.done {{
      background: rgba(76,175,80,0.14);
      color: #1f6a2b;
    }}
    .status-note.running {{
      background: rgba(255,186,73,0.20);
      color: #7d4a00;
    }}
    .status-note.failed {{
      background: rgba(178,34,34,0.10);
      color: #8d1c1c;
    }}
    .library {{
      margin-top: 8px;
      display: grid;
      gap: 12px;
    }}
    .library h2 {{
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      font-size: clamp(30px, 3vw, 40px);
    }}
    .library-empty {{
      color: var(--muted);
      font-size: clamp(18px, 2vw, 22px);
    }}
    .library-list {{
      display: grid;
      gap: 10px;
    }}
    .library-item {{
      padding: 14px 16px;
      border: 1px solid rgba(23, 20, 17, 0.10);
      border-radius: 20px;
      background: rgba(255,255,255,0.72);
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .library-name {{
      font-weight: 800;
      font-size: 16px;
    }}
    .library-meta {{
      color: var(--muted);
      font-size: 14px;
      word-break: break-word;
    }}
    .quick-card {{
      padding: 34px 36px 36px;
      border-radius: var(--radius);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.52), rgba(255,255,255,0.28)),
        var(--card);
      border: 1px solid rgba(208, 109, 45, 0.18);
      box-shadow: var(--shadow);
      display: grid;
      gap: 24px;
    }}
    .intro h1 {{
      margin: 0 0 10px;
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      font-size: clamp(52px, 6vw, 74px);
      line-height: 0.96;
      letter-spacing: -0.04em;
    }}
    .intro p {{
      margin: 0;
      max-width: 900px;
      font-size: clamp(18px, 2vw, 22px);
      line-height: 1.5;
      color: var(--muted);
    }}
    form {{
      display: grid;
      gap: 22px;
    }}
    .label-block {{
      display: grid;
      gap: 12px;
    }}
    .label-block strong {{
      font-size: 28px;
      font-weight: 900;
      letter-spacing: -0.02em;
      text-transform: uppercase;
    }}
    .field {{
      width: 100%;
      height: 92px;
      border-radius: 30px;
      border: 2px solid rgba(23, 20, 17, 0.10);
      background: rgba(255,255,255,0.84);
      padding: 0 28px;
      font-size: clamp(24px, 2.5vw, 30px);
      font-weight: 700;
      color: var(--ink);
      outline: none;
    }}
    .field::placeholder {{
      color: rgba(23, 20, 17, 0.5);
    }}
    .field:focus {{
      border-color: rgba(208, 109, 45, 0.55);
      box-shadow: 0 0 0 6px rgba(208, 109, 45, 0.10);
    }}
    .section-title {{
      font-size: clamp(24px, 2.6vw, 30px);
      color: var(--muted);
      margin: 0;
    }}
    .choice-row {{
      display: grid;
      gap: 20px;
    }}
    .choice {{
      position: relative;
    }}
    .choice input {{
      position: absolute;
      opacity: 0;
      pointer-events: none;
    }}
    .choice span {{
      display: block;
      padding: 28px 28px 30px;
      border-radius: 34px;
      border: 2px solid rgba(23, 20, 17, 0.10);
      background: rgba(255,255,255,0.72);
      cursor: pointer;
      transition: border-color 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
    }}
    .choice strong {{
      display: block;
      font-size: clamp(30px, 3vw, 42px);
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: -0.03em;
      margin-bottom: 12px;
    }}
    .choice small {{
      display: block;
      font-size: clamp(18px, 2vw, 22px);
      line-height: 1.45;
      color: rgba(23, 20, 17, 0.58);
      text-transform: uppercase;
      font-weight: 800;
    }}
    .choice input:checked + span {{
      border-color: rgba(208, 109, 45, 0.35);
      background: rgba(255, 245, 237, 0.96);
      box-shadow: 0 0 0 8px var(--accent-soft);
    }}
    .checks {{
      display: flex;
      gap: 30px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .checks label {{
      display: inline-flex;
      align-items: center;
      gap: 14px;
      font-size: clamp(20px, 2vw, 24px);
      font-weight: 700;
      color: var(--ink);
    }}
    .checks input {{
      width: 26px;
      height: 26px;
      margin: 0;
    }}
    .actions {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 18px;
      flex-wrap: wrap;
      padding-top: 4px;
    }}
    .cta {{
      border: none;
      border-radius: 999px;
      padding: 24px 40px;
      background: linear-gradient(135deg, #cf6a2f, #d97b22);
      color: #fffaf2;
      font-size: clamp(28px, 3vw, 40px);
      font-weight: 900;
      cursor: pointer;
      box-shadow: 0 18px 36px rgba(208, 109, 45, 0.20);
    }}
    .cta[disabled] {{
      cursor: wait;
      opacity: 0.78;
    }}
    .hint {{
      font-size: clamp(18px, 2vw, 22px);
      color: var(--muted);
    }}
    @media (max-width: 820px) {{
      body {{
        padding: 16px;
      }}
      .quick-card {{
        padding: 22px;
        gap: 18px;
      }}
      .field {{
        height: 76px;
        border-radius: 24px;
        padding: 0 20px;
      }}
      .choice span {{
        padding: 22px;
        border-radius: 26px;
      }}
      .actions {{
        align-items: flex-start;
      }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    {error_html}
    {dependency_html}
    <div id="job-banner" class="job-banner" aria-live="polite"></div>
    <section class="quick-card">
      <div class="intro">
        <h1>Tải nhanh</h1>
        <p>Đây là phần bạn nên dùng trong hầu hết trường hợp. Mặc định hệ thống sẽ ưu tiên chất lượng cao nhất.</p>
      </div>

      <form method="post" action="/jobs/download">
        <input type="hidden" name="mode" value="download">
        <input type="hidden" name="quality" value="best">
        <input type="hidden" name="remux_video" value="mp4">
        <input type="hidden" name="output_dir" value="downloads">

        <div class="label-block">
          <strong>Link video</strong>
          <input class="field" name="url" placeholder="Dán link video vào đây..." required>
        </div>

        <p class="section-title">Bạn muốn lấy kết quả theo kiểu nào?</p>

        <div class="choice-row">
          <label class="choice">
            <input type="radio" name="download_mode" value="video_best" checked>
            <span>
              <strong>Video gốc đẹp nhất</strong>
              <small>Giữ đúng tỷ lệ và chất lượng tốt nhất có thể.</small>
            </span>
          </label>
          <label class="choice">
            <input type="radio" name="download_mode" value="video_vertical">
            <span>
              <strong>Video dọc 9:16</strong>
              <small>Phù hợp TikTok, Shorts, Reels.</small>
            </span>
          </label>
          <label class="choice">
            <input type="radio" name="download_mode" value="audio_only">
            <span>
              <strong>Chỉ lấy âm thanh</strong>
              <small>Tải ra MP3 để nghe hoặc cắt ghép sau.</small>
            </span>
          </label>
        </div>

        <div class="checks">
          <label><input type="checkbox" name="write_thumbnail"> lưu ảnh thumbnail</label>
          <label><input type="checkbox" name="write_info_json"> lưu thông tin video</label>
        </div>

        <div class="actions">
          <button class="cta" type="submit">Tải video ngay</button>
          <div class="hint">Không cần đổi gì nếu bạn chỉ muốn tải video chất lượng cao.</div>
        </div>
      </form>

      <section class="status-panel">
        <h2>Trạng thái tải</h2>
        <div id="job-list"></div>
      </section>

      <section class="library">
        <h2>File đã tải</h2>
        <div id="recent-files"></div>
      </section>
    </section>
  </main>

  <script>
    const seededJobs = {jobs_json};
    const seededFiles = {recent_files_json};
    const form = document.querySelector("form");
    const modeInputs = document.querySelectorAll('input[name="download_mode"]');
    const submitButton = form.querySelector('button[type="submit"]');
    const defaultButtonLabel = submitButton.textContent;
    const statusSection = document.querySelector(".status-panel");
    const jobBanner = document.getElementById("job-banner");
    let lastHighlightedJobId = "";

    function syncMode() {{
      const selected = document.querySelector('input[name="download_mode"]:checked')?.value;
      const audioFlag = form.querySelector('input[name="audio_only"]');
      const presetField = form.querySelector('input[name="preset_name"]');
      const workflowField = form.querySelector('input[name="mode"]');

      if (!audioFlag) {{
        const audio = document.createElement("input");
        audio.type = "hidden";
        audio.name = "audio_only";
        form.appendChild(audio);
      }}
      if (!presetField) {{
        const preset = document.createElement("input");
        preset.type = "hidden";
        preset.name = "preset_name";
        form.appendChild(preset);
      }}

      const audioHidden = form.querySelector('input[name="audio_only"]');
      const presetHidden = form.querySelector('input[name="preset_name"]');

      audioHidden.value = "";
      presetHidden.value = "none";
      workflowField.value = "download";

      if (selected === "video_vertical") {{
        workflowField.value = "workflow";
        presetHidden.value = "reel";
      }}
      if (selected === "audio_only") {{
        audioHidden.value = "on";
      }}
    }}

    modeInputs.forEach((input) => input.addEventListener("change", syncMode));
    syncMode();

    form.addEventListener("submit", () => {{
      submitButton.disabled = true;
      submitButton.textContent = "Đang xử lý...";
      showBanner("running", "Đã nhận link. Hệ thống đang xử lý, trang sẽ tự cập nhật khi có kết quả.");
      statusSection.scrollIntoView({ behavior: "smooth", block: "start" });
    }});

    function escapeHtml(value) {{
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}

    function showBanner(kind, message) {{
      jobBanner.className = `job-banner show ${kind}`;
      jobBanner.textContent = message;
    }}

    function updateBannerFromJobs(items) {{
      const latest = items[0];
      if (!latest) {{
        return;
      }}
      if (latest.status === "running") {{
        showBanner("running", "Đang xử lý video. Chờ một chút, khi xong sẽ hiện nút lưu vào máy.");
        return;
      }}
      if (latest.status === "failed") {{
        showBanner("failed", "Lần tải gần nhất chưa thành công. Kéo xuống xem log rồi thử lại.");
        submitButton.disabled = false;
        submitButton.textContent = defaultButtonLabel;
        return;
      }}
      if (latest.downloadable_url) {{
        showBanner("done", "Xong rồi. Kéo xuống ngay bên dưới và bấm nút Lưu vào máy.");
        submitButton.disabled = false;
        submitButton.textContent = defaultButtonLabel;
        if (lastHighlightedJobId !== latest.id) {{
          lastHighlightedJobId = latest.id;
          statusSection.scrollIntoView({ behavior: "smooth", block: "start" });
        }}
        return;
      }}
      submitButton.disabled = false;
      submitButton.textContent = defaultButtonLabel;
    }}

    function renderJobs(items) {{
      const root = document.getElementById("job-list");
      if (!items.length) {{
        root.innerHTML = '<div class="status-empty">Chưa có lần tải nào. Dán link rồi bấm nút để bắt đầu.</div>';
        submitButton.disabled = false;
        submitButton.textContent = defaultButtonLabel;
        return;
      }}
      updateBannerFromJobs(items);
      root.innerHTML = items.map((job) => {{
        const logs = (job.log_lines || []).join("\\n");
        const output = job.downloadable_path || job.output_path || "se hien khi co";
        const statusNote = job.downloadable_url
          ? '<div class="status-note done">Xong rồi, bấm nút bên dưới để lưu video vào máy.</div>'
          : job.status === "running"
            ? '<div class="status-note running">Hệ thống đang xử lý. Chờ một chút, khi xong sẽ hiện nút lưu vào máy.</div>'
            : job.status === "failed"
              ? '<div class="status-note failed">Lần tải này chưa thành công. Bạn có thể kiểm tra log bên dưới rồi thử lại.</div>'
              : "";
        const linkHtml = job.downloadable_url
          ? `<a class="download-link primary" href="${escapeHtml(job.downloadable_url)}" download>Lưu vào máy</a>`
          : "";
        return `
          <article class="status-item">
            <div class="status-head">
              <div>
                <div class="status-title">${escapeHtml(job.title)}</div>
                <div class="status-meta">${escapeHtml(job.created_label || "")} · exit ${escapeHtml(job.return_code ?? "-")}</div>
              </div>
              <span class="status-badge ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span>
            </div>
            <div class="status-body">
              <div class="status-path"><strong>File sẽ nằm ở:</strong> ${escapeHtml(output)}</div>
              ${statusNote}
              ${linkHtml}
              <pre class="status-log">${escapeHtml(logs || "Đang chờ log...")}</pre>
            </div>
          </article>
        `;
      }}).join("");
    }}

    function renderRecentFiles(items) {{
      const root = document.getElementById("recent-files");
      if (!items.length) {{
        root.innerHTML = '<div class="library-empty">Chưa có file nào trong thư mục downloads.</div>';
        return;
      }}
      root.innerHTML = `
        <div class="library-list">
          ${items.map((file) => `
            <article class="library-item">
              <div>
                <div class="library-name">${escapeHtml(file.name)}</div>
                <div class="library-meta">${escapeHtml(file.size)} · ${escapeHtml(file.path)}</div>
              </div>
              <a class="download-link" href="${escapeHtml(file.url)}" download>Tải file này</a>
            </article>
          `).join("")}
        </div>
      `;
    }}

    renderJobs(seededJobs);
    renderRecentFiles(seededFiles);

    async function refreshJobs() {{
      try {{
        const response = await fetch("/api/jobs", { cache: "no-store" });
        if (!response.ok) return;
        renderJobs(await response.json());
      }} catch (_error) {{
      }}
    }}

    async function refreshFiles() {{
      try {{
        const response = await fetch("/api/files", { cache: "no-store" });
        if (!response.ok) return;
        renderRecentFiles(await response.json());
      }} catch (_error) {{
      }}
    }}

    setInterval(refreshJobs, 2500);
    setInterval(refreshFiles, 4000);
  </script>
</body>
</html>"""


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.respond_html(render_page())
            return
        if parsed.path == "/api/jobs":
            with jobs_lock:
                payload = [job.to_dict() for job in sorted(jobs.values(), key=lambda item: item.created_at, reverse=True)[:20]]
            self.respond_json(payload)
            return
        if parsed.path == "/healthz":
            self.respond_json({"ok": True})
            return
        if parsed.path == "/api/files":
            self.respond_json(list_recent_downloads())
            return
        if parsed.path.startswith("/downloads/"):
            requested = (ROOT_DIR / parsed.path.lstrip("/")).resolve()
            if not str(requested).startswith(str(DOWNLOADS_DIR.resolve())) or not requested.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.serve_file(requested)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        raw_form = urllib.parse.parse_qs(body, keep_blank_values=True)
        form = {key: values[-1] for key, values in raw_form.items()}

        try {{
            if parsed.path == "/jobs/download":
                create_download_job(form)
            elif parsed.path == "/jobs/batch":
                create_batch_job(form)
            elif parsed.path == "/jobs/edit":
                create_edit_job(form)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
        except ValueError as error:
            self.respond_html(render_page(str(error)), status=HTTPStatus.BAD_REQUEST)
            return

        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def respond_html(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def respond_json(self, payload: object) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def serve_file(self, path: Path) -> None:
        content_type, _encoding = mimetypes.guess_type(path.name)
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> int:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    port = get_listen_port()
    server = ThreadingHTTPServer((HOST, port), AppHandler)
    lan_ip = detect_lan_ip()
    print(f"Video Forge Desk running at http://127.0.0.1:{port}")
    print(f"LAN access: http://{lan_ip}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
