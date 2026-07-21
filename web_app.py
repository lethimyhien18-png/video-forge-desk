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
        payload["display_title"] = build_job_display_title(self)
        payload["display_file_name"] = Path(self.downloadable_path or self.output_path).name if (self.downloadable_path or self.output_path) else ""
        return payload


jobs: Dict[str, Job] = {}
jobs_lock = threading.Lock()


def truncate_text(value: str, limit: int = 72) -> str:
    cleaned = " ".join((value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def build_job_display_title(job: Job) -> str:
    if job.downloadable_path:
        return truncate_text(Path(job.downloadable_path).stem, 78)
    source = job.title.replace("workflow:", "").replace("download:", "").replace("edit:", "").strip()
    if source.startswith("http://") or source.startswith("https://"):
        parsed = urllib.parse.urlparse(source)
        compact = parsed.netloc + (parsed.path or "")
        return truncate_text(compact, 64)
    return truncate_text(source, 78)


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
        if item.is_file()
        and not item.name.startswith(".")
        and item.suffix.lower() not in {".json", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt", ".txt"}
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime)


def list_recent_downloads(limit: int = 10) -> List[Dict[str, str]]:
    if not DOWNLOADS_DIR.exists():
        return []
    files = [
        item for item in DOWNLOADS_DIR.rglob("*")
        if item.is_file()
        and not item.name.startswith(".")
        and item.suffix.lower() not in {".json", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt", ".txt"}
    ]
    files.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    results: List[Dict[str, str]] = []
    for item in files[:limit]:
        try:
            relative = item.resolve().relative_to(ROOT_DIR.resolve())
            url = "/downloads/" + urllib.parse.quote(str(relative).replace(os.sep, "/").removeprefix("downloads/"))
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
            latest.downloadable_url = f"/download/{job_id}"


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
  <title>hungphatluxury</title>
  <style>
    :root {{
      --bg: #f6f0e8;
      --bg-2: #efe3d1;
      --ink: #1f1812;
      --muted: rgba(31, 24, 18, 0.62);
      --line: rgba(31, 24, 18, 0.10);
      --accent: #b77933;
      --accent-2: #dfb16f;
      --accent-soft: rgba(183, 121, 51, 0.12);
      --card: rgba(255, 252, 247, 0.82);
      --radius: 32px;
      --shadow: 0 26px 60px rgba(95, 61, 26, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(183, 121, 51, 0.16), transparent 28%),
        radial-gradient(circle at top right, rgba(223, 177, 111, 0.18), transparent 24%),
        linear-gradient(180deg, var(--bg) 0%, var(--bg-2) 100%);
      padding: 20px;
    }}
    .shell {{
      width: min(980px, 100%);
      margin: 0 auto;
    }}
    .brand {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 18px;
      padding: 10px 4px 0;
    }}
    .brand-mark {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 46px;
      height: 46px;
      border-radius: 999px;
      background: linear-gradient(135deg, #fff8ef, #f0d7b1);
      border: 1px solid rgba(183, 121, 51, 0.18);
      box-shadow: 0 10px 24px rgba(95, 61, 26, 0.10);
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: 0.08em;
      color: #8f5b24;
    }}
    .brand-copy {{
      display: grid;
      gap: 2px;
      flex: 1;
    }}
    .brand-name {{
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: -0.03em;
    }}
    .brand-sub {{
      font-size: 14px;
      color: var(--muted);
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
      font-size: 20px;
      font-weight: 800;
      line-height: 1.35;
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
    .save-callout {{
      padding: 18px;
      border-radius: 22px;
      background: linear-gradient(180deg, rgba(255,248,239,0.98), rgba(255,241,225,0.94));
      border: 1px solid rgba(208, 109, 45, 0.22);
      display: grid;
      gap: 12px;
    }}
    .save-callout strong {{
      font-size: clamp(22px, 3vw, 28px);
      line-height: 1.1;
    }}
    .save-callout p {{
      margin: 0;
      color: #855022;
      font-size: 15px;
      line-height: 1.5;
      font-weight: 700;
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
    .result-card {{
      padding: 16px 18px;
      border-radius: 20px;
      background: rgba(255,255,255,0.74);
      border: 1px solid rgba(31, 24, 18, 0.08);
      display: grid;
      gap: 8px;
    }}
    .result-label {{
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: #8f5b24;
    }}
    .result-name {{
      font-size: 17px;
      font-weight: 800;
      line-height: 1.45;
      word-break: break-word;
      color: #2a2018;
    }}
    .technical-toggle {{
      margin-top: 2px;
      border-top: 1px solid rgba(31, 24, 18, 0.08);
      padding-top: 12px;
    }}
    .technical-toggle summary {{
      cursor: pointer;
      list-style: none;
      font-weight: 800;
      color: #7f5527;
      font-size: 14px;
    }}
    .technical-toggle summary::-webkit-details-marker {{
      display: none;
    }}
    .technical-panel {{
      display: grid;
      gap: 10px;
      margin-top: 12px;
    }}
    .technical-path {{
      font-size: 14px;
      color: var(--muted);
      word-break: break-word;
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
      font-size: 15px;
      line-height: 1.45;
      word-break: break-word;
    }}
    .library-meta {{
      color: var(--muted);
      font-size: 13px;
      word-break: break-word;
    }}
    .history-toggle {{
      margin-top: 6px;
      border-top: 1px solid rgba(31, 24, 18, 0.08);
      padding-top: 12px;
    }}
    .history-toggle summary {{
      cursor: pointer;
      list-style: none;
      font-weight: 800;
      color: #7f5527;
    }}
    .history-toggle summary::-webkit-details-marker {{
      display: none;
    }}
    .history-list {{
      display: grid;
      gap: 10px;
      margin-top: 12px;
    }}
    .history-mini {{
      padding: 12px 14px;
      border-radius: 16px;
      border: 1px solid rgba(31, 24, 18, 0.08);
      background: rgba(255,255,255,0.66);
      display: grid;
      gap: 8px;
    }}
    .history-mini strong {{
      font-size: 15px;
    }}
    .history-mini .status-meta {{
      font-size: 13px;
    }}
    .quick-card {{
      padding: 32px;
      border-radius: var(--radius);
      background:
        linear-gradient(180deg, rgba(255,255,255,0.70), rgba(255,255,255,0.34)),
        var(--card);
      border: 1px solid rgba(183, 121, 51, 0.15);
      box-shadow: var(--shadow);
      display: grid;
      gap: 22px;
    }}
    .intro h1 {{
      margin: 0 0 8px;
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      font-size: clamp(40px, 6vw, 58px);
      line-height: 0.98;
      letter-spacing: -0.04em;
    }}
    .intro p {{
      margin: 0;
      max-width: 640px;
      font-size: clamp(16px, 2vw, 19px);
      line-height: 1.5;
      color: var(--muted);
    }}
    .hero-points {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 16px;
    }}
    .hero-points span {{
      display: inline-flex;
      align-items: center;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(255,255,255,0.72);
      border: 1px solid rgba(183, 121, 51, 0.14);
      color: #7f5527;
      font-size: 14px;
      font-weight: 700;
    }}
    form {{
      display: grid;
      gap: 18px;
    }}
    .label-block {{
      display: grid;
      gap: 10px;
    }}
    .label-block strong {{
      font-size: 14px;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: #8f5b24;
    }}
    .field {{
      width: 100%;
      height: 78px;
      border-radius: 24px;
      border: 2px solid rgba(23, 20, 17, 0.10);
      background: rgba(255,255,255,0.92);
      padding: 0 22px;
      font-size: clamp(18px, 2.2vw, 22px);
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
    .checks {{
      display: flex;
      gap: 18px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .checks label {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-size: clamp(15px, 1.8vw, 17px);
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
      flex-direction: column;
      align-items: stretch;
      gap: 10px;
      flex-wrap: wrap;
      padding-top: 8px;
    }}
    .cta {{
      border: none;
      border-radius: 999px;
      width: 100%;
      padding: 20px 28px;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      color: #fffaf2;
      font-size: clamp(22px, 3vw, 28px);
      font-weight: 900;
      cursor: pointer;
      box-shadow: 0 18px 36px rgba(183, 121, 51, 0.24);
    }}
    .cta[disabled] {{
      cursor: wait;
      opacity: 0.78;
    }}
    .hint {{
      text-align: center;
      font-size: clamp(14px, 1.8vw, 16px);
      color: var(--muted);
    }}
    .sections-grid {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 18px;
    }}
    .section-card {{
      padding: 22px;
      border-radius: 24px;
      background: rgba(255,255,255,0.58);
      border: 1px solid rgba(31, 24, 18, 0.08);
    }}
    @media (max-width: 820px) {{
      body {{
        padding: 16px;
      }}
      .brand {{
        align-items: flex-start;
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
        padding: 18px;
      }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    {error_html}
    {dependency_html}
    <div class="brand">
      <div class="brand-mark">H</div>
      <div class="brand-copy">
        <div class="brand-name">hungphatluxury</div>
        <div class="brand-sub">Tải video nhanh, đẹp, gọn và dễ dùng.</div>
      </div>
    </div>
    <div id="job-banner" class="job-banner" aria-live="polite"></div>
    <section class="quick-card">
      <div class="intro">
        <h1>Tải video chỉ với 1 link</h1>
        <p>Dán link, bấm tải, chờ vài giây rồi bấm lưu về máy. Mặc định luôn ưu tiên bản đẹp nhất và dễ dùng trên điện thoại.</p>
        <div class="hero-points">
          <span>Ưu tiên chất lượng cao</span>
          <span>Dùng tốt trên điện thoại</span>
          <span>Không cần biết kỹ thuật</span>
        </div>
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

        <div class="actions">
          <button class="cta" type="submit">Tải video ngay</button>
        </div>
      </form>

      <div class="sections-grid">
        <section class="status-panel section-card">
          <h2>Trạng thái tải</h2>
          <div id="job-list"></div>
        </section>

        <section class="library section-card">
          <h2>File đã tải</h2>
          <div id="recent-files"></div>
        </section>
      </div>
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
      statusSection.scrollIntoView({{ behavior: "smooth", block: "start" }});
    }});

    function escapeHtml(value) {{
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }}

    function showBanner(kind, message) {{
      jobBanner.className = `job-banner show ${{kind}}`;
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
          statusSection.scrollIntoView({{ behavior: "smooth", block: "start" }});
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
      const latest = items[0];
      const history = items.slice(1, 4);
      const latestHtml = latest ? (() => {{
        const logs = (latest.log_lines || []).join("\\n");
        const output = latest.downloadable_path || latest.output_path || "Sẽ hiện khi xử lý xong";
        const displayTitle = latest.display_title || latest.title || "Video đã tải";
        const displayFileName = latest.display_file_name || "";
        const saveCallout = latest.downloadable_url
          ? `
            <div class="save-callout">
              <strong>Xong rồi, bấm đây để lưu vào máy</strong>
              <p>Sau khi bấm nút bên dưới, máy sẽ mở phần lưu file hoặc tải xuống.</p>
              <a class="download-link primary" href="${{escapeHtml(latest.downloadable_url)}}" download>Lưu vào máy</a>
            </div>
          `
          : "";
        const statusNote = latest.downloadable_url
          ? '<div class="status-note done">Video đã sẵn sàng. Bạn chỉ cần bấm nút Lưu vào máy.</div>'
          : latest.status === "running"
            ? '<div class="status-note running">Đang xử lý video. Chờ thêm một chút, nút lưu sẽ hiện ngay tại đây.</div>'
            : latest.status === "failed"
              ? '<div class="status-note failed">Lần tải này chưa thành công. Hãy kiểm tra log bên dưới rồi thử lại.</div>'
              : "";
        return `
          <article class="status-item">
            <div class="status-head">
              <div>
                <div class="status-title">${{escapeHtml(displayTitle)}}</div>
                <div class="status-meta">${{escapeHtml(latest.created_label || "")}}</div>
              </div>
              <span class="status-badge ${{escapeHtml(latest.status)}}">${{escapeHtml(latest.status)}}</span>
            </div>
            <div class="status-body">
              ${{displayFileName ? `
                <div class="result-card">
                  <div class="result-label">Video đã sẵn sàng</div>
                  <div class="result-name">${{escapeHtml(displayFileName)}}</div>
                </div>
              ` : ""}}
              ${{saveCallout}}
              ${{statusNote}}
              <details class="technical-toggle">
                <summary>Xem chi tiết kỹ thuật</summary>
                <div class="technical-panel">
                  <div class="technical-path"><strong>Đường dẫn file:</strong> ${{escapeHtml(output)}}</div>
                  <pre class="status-log">${{escapeHtml(logs || "Đang chờ log...")}}</pre>
                </div>
              </details>
            </div>
          </article>
        `;
      }})() : "";
      const historyHtml = history.length
        ? `
          <details class="history-toggle">
            <summary>Xem các lần tải trước</summary>
            <div class="history-list">
              ${{history.map((job) => {{
                const linkHtml = job.downloadable_url
                  ? `<a class="download-link" href="${{escapeHtml(job.downloadable_url)}}" download>Tải lại file</a>`
                  : "";
                return `
                  <article class="history-mini">
                    <strong>${{escapeHtml(job.display_title || job.title)}}</strong>
                    <div class="status-meta">${{escapeHtml(job.created_label || "")}} · ${{escapeHtml(job.status)}}</div>
                    ${{linkHtml}}
                  </article>
                `;
              }}).join("")}}
            </div>
          </details>
        `
        : "";
      root.innerHTML = `
        ${{latestHtml}}
        ${{historyHtml}}
      `;
    }}

    function renderRecentFiles(items) {{
      const root = document.getElementById("recent-files");
      if (!items.length) {{
        root.innerHTML = '<div class="library-empty">Chưa có file nào trong thư mục downloads.</div>';
        return;
      }}
      root.innerHTML = `
        <div class="library-list">
          ${{items.map((file) => `
            <article class="library-item">
              <div>
                <div class="library-name">${{escapeHtml(file.name)}}</div>
                <div class="library-meta">${{escapeHtml(file.size)}}</div>
              </div>
              <a class="download-link" href="${{escapeHtml(file.url)}}" download>Tải file này</a>
            </article>
          `).join("")}}
        </div>
      `;
    }}

    renderJobs(seededJobs);
    renderRecentFiles(seededFiles);

    async function refreshJobs() {{
      try {{
        const response = await fetch("/api/jobs", {{ cache: "no-store" }});
        if (!response.ok) return;
        renderJobs(await response.json());
      }} catch (_error) {{
      }}
    }}

    async function refreshFiles() {{
      try {{
        const response = await fetch("/api/files", {{ cache: "no-store" }});
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


def render_download_missing_page() -> str:
    return """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Không tìm thấy file tải</title>
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background: linear-gradient(180deg, #f7f1e8 0%, #efe2cf 100%);
      color: #241a12;
    }
    .card {
      width: min(560px, 100%);
      padding: 28px;
      border-radius: 24px;
      background: rgba(255, 252, 248, 0.94);
      border: 1px solid rgba(36, 26, 18, 0.10);
      box-shadow: 0 24px 50px rgba(100, 65, 30, 0.12);
    }
    h1 {
      margin: 0 0 10px;
      font-size: 32px;
      line-height: 1.05;
    }
    p {
      margin: 0 0 14px;
      font-size: 17px;
      line-height: 1.6;
      color: rgba(36, 26, 18, 0.72);
    }
    a {
      display: inline-flex;
      margin-top: 10px;
      padding: 14px 18px;
      border-radius: 999px;
      background: #c87432;
      color: white;
      text-decoration: none;
      font-weight: 800;
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>File tải không còn sẵn sàng</h1>
    <p>Liên kết này đã hết hiệu lực hoặc file đã không còn trên máy chủ.</p>
    <p>Hãy quay lại trang chính, dán link video và tải lại một lần nữa để tạo file mới.</p>
    <a href="/">Quay lại trang tải video</a>
  </div>
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
        if parsed.path.startswith("/download/"):
            job_id = parsed.path.removeprefix("/download/").strip("/")
            if not job_id:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            with jobs_lock:
                job = jobs.get(job_id)
                target_path = job.downloadable_path if job else ""
            if not target_path:
                self.respond_html(render_download_missing_page(), status=HTTPStatus.GONE)
                return
            target = Path(target_path)
            if not target.exists() or not target.is_file():
                self.respond_html(render_download_missing_page(), status=HTTPStatus.GONE)
                return
            self.serve_file(target)
            return
        if parsed.path.startswith("/downloads/"):
            relative_path = urllib.parse.unquote(parsed.path.removeprefix("/downloads/"))
            requested = (DOWNLOADS_DIR / relative_path).resolve()
            if not str(requested).startswith(str(DOWNLOADS_DIR.resolve())) or not requested.is_file():
                self.respond_html(render_download_missing_page(), status=HTTPStatus.GONE)
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

        try:
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
        quoted_name = urllib.parse.quote(path.name)
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted_name}")
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
