#!/usr/bin/env python3
"""
CLI tool for downloading videos from common social platforms and applying
basic edits with ffmpeg.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


SUPPORTED_SITES = ("x", "tiktok", "facebook", "youtube")
FALLBACK_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
QUALITY_MAP = {
    "best": "bestvideo*+bestaudio/best",
    "up-to-1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
    "up-to-720": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
}
EDIT_PRESETS = {
    "none": {},
    "reel": {"resize": "1080:1920"},
    "shorts": {"resize": "1080:1920"},
    "story": {"resize": "1080:1920"},
    "square": {"resize": "1080:1080"},
    "variation": {
        "video_filter": "scale=iw*1.04:ih*1.04,crop=iw/1.04:ih/1.04",
        "start": "2",
    },
}
BUILTIN_MUSIC_TRACKS = {
    "soft": "assets/music/soft-breeze.mp3",
    "warm": "assets/music/warm-glow.mp3",
    "bright": "assets/music/bright-morning.mp3",
}
FFMPEG_FILTER_CACHE: dict[str, bool] = {}


@dataclass
class EditOptions:
    start: Optional[str] = None
    end: Optional[str] = None
    duration: Optional[str] = None
    crop: Optional[str] = None
    resize: Optional[str] = None
    video_filter: Optional[str] = None
    overlay_text: Optional[str] = None
    bg_music_track: Optional[str] = None
    bg_music_volume: float = 0.16
    video_codec: str = "libx264"
    crf: int = 18
    preset: str = "slow"
    mute: bool = False
    extract_audio: bool = False
    denoise_audio: bool = False
    beautify: bool = False
    faststart: bool = True


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download videos from X/TikTok/Facebook/YouTube and edit them.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    download = subparsers.add_parser("download", help="Download a single video URL")
    download.add_argument("url", help="Video URL from a supported platform")
    download.add_argument(
        "--output-dir",
        default="downloads",
        help="Directory to store downloaded files",
    )
    download.add_argument(
        "--filename",
        default="%(uploader|unknown)s/%(upload_date>%Y-%m-%d)s_%(title).180B_[%(id)s].%(ext)s",
        help="Output filename template for yt-dlp",
    )
    download.add_argument(
        "--cookies",
        help="Optional Netscape cookie file for private or rate-limited videos",
    )
    download.add_argument(
        "--audio-only",
        action="store_true",
        help="Download audio only",
    )
    download.add_argument(
        "--quality",
        choices=("best", "up-to-1080", "up-to-720"),
        default="best",
        help="Preferred download quality. Default keeps the highest quality available.",
    )
    download.add_argument(
        "--remux-video",
        choices=("mp4", "mkv", "webm"),
        default="mp4",
        help="Remux downloaded video into a cleaner container when possible.",
    )
    download.add_argument(
        "--write-thumbnail",
        action="store_true",
        help="Save thumbnail when available.",
    )
    download.add_argument(
        "--write-info-json",
        action="store_true",
        help="Save metadata JSON when available.",
    )
    download.add_argument(
        "--no-archive",
        action="store_true",
        help="Allow redownloading the same video again.",
    )
    download.add_argument(
        "--subs",
        action="store_true",
        help="Download subtitles when available",
    )
    download.add_argument(
        "--print-command",
        action="store_true",
        help="Only print the generated command without running it",
    )

    edit = subparsers.add_parser("edit", help="Apply basic edits using ffmpeg")
    edit.add_argument("input_file", help="Path to the input video or audio file")
    edit.add_argument(
        "--output",
        help="Output path. Defaults to <input_stem>_edited<suffix>",
    )
    edit.add_argument("--start", help="Trim start time, e.g. 00:00:05")
    edit.add_argument("--end", help="Trim end time, e.g. 00:00:18")
    edit.add_argument("--duration", help="Trim duration, e.g. 15")
    edit.add_argument(
        "--preset-name",
        choices=tuple(EDIT_PRESETS.keys()),
        default="none",
        help="Quick edit preset for common social output formats.",
    )
    edit.add_argument(
        "--crop",
        help="Crop filter in ffmpeg syntax, e.g. 1080:1080:420:0",
    )
    edit.add_argument(
        "--resize",
        help="Resize in WIDTH:HEIGHT format, e.g. 1080:1920",
    )
    edit.add_argument(
        "--video-filter",
        help="Extra ffmpeg video filter chain, e.g. scale=iw*1.04:ih*1.04,crop=iw/1.04:ih/1.04",
    )
    edit.add_argument(
        "--overlay-text",
        help="Running text shown on the video, e.g. 'Giảm giá cuối tuần'",
    )
    edit.add_argument(
        "--bg-music-track",
        choices=("soft", "warm", "bright"),
        help="Add one built-in light background music track",
    )
    edit.add_argument(
        "--bg-music-volume",
        type=float,
        default=0.16,
        help="Background music volume from 0.0 to 1.0. Default 0.16.",
    )
    edit.add_argument(
        "--mute",
        action="store_true",
        help="Remove audio from the output video",
    )
    edit.add_argument(
        "--denoise-audio",
        action="store_true",
        help="Apply a light ffmpeg noise reduction filter to the audio track",
    )
    edit.add_argument(
        "--beautify",
        action="store_true",
        help="Apply light contrast, saturation, and sharpening for a cleaner look",
    )
    edit.add_argument(
        "--video-codec",
        choices=("libx264", "libx265"),
        default="libx264",
        help="Video codec used when re-encoding is required.",
    )
    edit.add_argument(
        "--crf",
        type=int,
        default=18,
        help="Quality factor for encoded video. Lower is higher quality. Default 18.",
    )
    edit.add_argument(
        "--encode-preset",
        default="slow",
        help="ffmpeg encoding preset when re-encoding is required.",
    )
    edit.add_argument(
        "--extract-audio",
        action="store_true",
        help="Export audio only as mp3",
    )
    edit.add_argument(
        "--print-command",
        action="store_true",
        help="Only print the generated command without running it",
    )

    workflow = subparsers.add_parser(
        "workflow",
        help="Download first, then optionally edit the downloaded file",
    )
    workflow.add_argument("url", help="Video URL from a supported platform")
    workflow.add_argument("--output-dir", default="downloads")
    workflow.add_argument(
        "--filename",
        default="%(uploader|unknown)s/%(upload_date>%Y-%m-%d)s_%(title).180B_[%(id)s].%(ext)s",
    )
    workflow.add_argument("--cookies")
    workflow.add_argument("--audio-only", action="store_true")
    workflow.add_argument(
        "--quality",
        choices=("best", "up-to-1080", "up-to-720"),
        default="best",
    )
    workflow.add_argument(
        "--remux-video",
        choices=("mp4", "mkv", "webm"),
        default="mp4",
    )
    workflow.add_argument("--write-thumbnail", action="store_true")
    workflow.add_argument("--write-info-json", action="store_true")
    workflow.add_argument("--no-archive", action="store_true")
    workflow.add_argument("--subs", action="store_true")
    workflow.add_argument("--start")
    workflow.add_argument("--end")
    workflow.add_argument("--duration")
    workflow.add_argument(
        "--preset-name",
        choices=tuple(EDIT_PRESETS.keys()),
        default="none",
    )
    workflow.add_argument("--crop")
    workflow.add_argument("--resize")
    workflow.add_argument("--video-filter")
    workflow.add_argument("--overlay-text")
    workflow.add_argument("--bg-music-track", choices=("soft", "warm", "bright"))
    workflow.add_argument("--bg-music-volume", type=float, default=0.16)
    workflow.add_argument("--mute", action="store_true")
    workflow.add_argument("--denoise-audio", action="store_true")
    workflow.add_argument("--beautify", action="store_true")
    workflow.add_argument(
        "--video-codec",
        choices=("libx264", "libx265"),
        default="libx264",
    )
    workflow.add_argument("--crf", type=int, default=18)
    workflow.add_argument("--encode-preset", default="slow")
    workflow.add_argument("--extract-audio", action="store_true")
    workflow.add_argument("--print-command", action="store_true")

    batch = subparsers.add_parser(
        "batch",
        help="Download multiple URLs from a text file, one URL per line",
    )
    batch.add_argument("input_file", help="Text file containing URLs")
    batch.add_argument("--output-dir", default="downloads")
    batch.add_argument(
        "--filename",
        default="%(uploader|unknown)s/%(upload_date>%Y-%m-%d)s_%(title).180B_[%(id)s].%(ext)s",
    )
    batch.add_argument("--cookies")
    batch.add_argument("--audio-only", action="store_true")
    batch.add_argument(
        "--quality",
        choices=("best", "up-to-1080", "up-to-720"),
        default="best",
    )
    batch.add_argument(
        "--remux-video",
        choices=("mp4", "mkv", "webm"),
        default="mp4",
    )
    batch.add_argument("--write-thumbnail", action="store_true")
    batch.add_argument("--write-info-json", action="store_true")
    batch.add_argument("--no-archive", action="store_true")
    batch.add_argument("--subs", action="store_true")
    batch.add_argument("--print-command", action="store_true")

    inspect = subparsers.add_parser(
        "inspect",
        help="Show dependency status and supported features",
    )
    inspect.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable dependency info",
    )

    return parser.parse_args(argv)


def require_binary(binary_name: str) -> str:
    search_path = build_search_path()
    binary_path = shutil.which(binary_name, path=search_path)
    if not binary_path:
        raise RuntimeError(
            f"Missing required dependency: {binary_name}. "
            f"Install it first, then rerun the command."
        )
    return binary_path


def build_search_path() -> str:
    search_path = os.environ.get("PATH", "")
    for bin_dir in FALLBACK_BIN_DIRS:
        if bin_dir not in search_path.split(os.pathsep):
            search_path = f"{bin_dir}{os.pathsep}{search_path}" if search_path else bin_dir
    return search_path


def build_runtime_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = build_search_path()
    return env


def detect_platform(url: str) -> str:
    lowered = url.lower()
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "youtube"
    if "tiktok.com" in lowered:
        return "tiktok"
    if "facebook.com" in lowered or "fb.watch" in lowered:
        return "facebook"
    if "x.com" in lowered or "twitter.com" in lowered:
        return "x"
    return "unknown"


def build_download_command(args: argparse.Namespace) -> List[str]:
    yt_dlp_path = require_binary("yt-dlp")
    ffmpeg_path = require_binary("ffmpeg")
    output_dir = Path(args.output_dir)
    output_template = str(output_dir / args.filename)
    command = [
        yt_dlp_path,
        args.url,
        "-o",
        output_template,
        "--newline",
        "--progress",
        "--continue",
        "--no-overwrites",
        "--concurrent-fragments",
        "4",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--ffmpeg-location",
        str(Path(ffmpeg_path).parent),
    ]

    if args.audio_only:
        command.extend(["-x", "--audio-format", "mp3", "--audio-quality", "0"])
    else:
        command.extend(
            [
                "-f",
                QUALITY_MAP[args.quality],
                "--merge-output-format",
                args.remux_video,
                "--remux-video",
                args.remux_video,
            ]
        )

    if not args.no_archive:
        archive_path = output_dir / ".download-archive.txt"
        command.extend(["--download-archive", str(archive_path)])

    if args.cookies:
        command.extend(["--cookies", args.cookies])

    if args.subs:
        command.extend(["--write-auto-subs", "--sub-langs", "all"])

    if args.write_thumbnail:
        command.append("--write-thumbnail")

    if args.write_info_json:
        command.append("--write-info-json")

    return command


def resolve_edit_output(input_file: Path, output: Optional[str], extract_audio: bool) -> Path:
    if output:
        return Path(output)
    suffix = ".mp3" if extract_audio else input_file.suffix
    return input_file.with_name(f"{input_file.stem}_edited{suffix}")


def resolve_bg_music_track(track_name: Optional[str]) -> Optional[Path]:
    if not track_name:
        return None
    relative_path = BUILTIN_MUSIC_TRACKS.get(track_name)
    if not relative_path:
        raise RuntimeError(f"Unknown background music track: {track_name}")
    music_path = Path(__file__).resolve().parent / relative_path
    if not music_path.exists():
        raise RuntimeError(f"Missing built-in background music track: {music_path}")
    return music_path


def input_has_audio(input_file: Path) -> bool:
    ffprobe_path = require_binary("ffprobe")
    completed = subprocess.run(
        [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(input_file),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=build_runtime_env(),
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def ffmpeg_has_filter(filter_name: str) -> bool:
    if filter_name in FFMPEG_FILTER_CACHE:
        return FFMPEG_FILTER_CACHE[filter_name]
    ffmpeg_path = require_binary("ffmpeg")
    completed = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-filters"],
        check=False,
        capture_output=True,
        text=True,
        env=build_runtime_env(),
    )
    supported = completed.returncode == 0 and filter_name in completed.stdout
    FFMPEG_FILTER_CACHE[filter_name] = supported
    return supported


def escape_drawtext(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def build_video_filters(options: EditOptions) -> List[str]:
    filters: List[str] = []
    if options.video_filter:
        filters.append(options.video_filter)
    if options.crop:
        filters.append(f"crop={options.crop}")
    if options.resize:
        filters.append(f"scale={options.resize}")
    if options.beautify:
        filters.append("eq=contrast=1.05:brightness=0.02:saturation=1.12")
        filters.append("unsharp=5:5:0.6:3:3:0.2")
    if options.overlay_text and ffmpeg_has_filter("drawtext"):
        overlay_text = escape_drawtext(options.overlay_text)
        filters.append(
            "drawtext="
            f"text='{overlay_text}':"
            "fontcolor=white:"
            "fontsize=h*0.045:"
            "box=1:"
            "boxcolor=black@0.30:"
            "boxborderw=18:"
            "x=w-mod(max(t\\,0)*(w*0.18)\\,(text_w+w))-text_w:"
            "y=h*0.84"
        )
    return filters


def build_audio_filters(options: EditOptions) -> List[str]:
    filters: List[str] = []
    if options.denoise_audio:
        filters.append("afftdn=nf=-22")
    if options.beautify and not options.mute:
        filters.append("volume=1.15")
    return filters


def clamp_music_volume(volume: float) -> float:
    return max(0.03, min(volume, 0.45))


def apply_edit_preset(options: EditOptions, preset_name: str) -> EditOptions:
    preset = EDIT_PRESETS[preset_name]
    merged = EditOptions(
        start=options.start,
        end=options.end,
        duration=options.duration,
        crop=options.crop,
        resize=options.resize or preset.get("resize"),
        video_filter=options.video_filter or preset.get("video_filter"),
        overlay_text=options.overlay_text,
        bg_music_track=options.bg_music_track,
        bg_music_volume=options.bg_music_volume,
        video_codec=options.video_codec,
        crf=options.crf,
        preset=options.preset,
        mute=options.mute,
        extract_audio=options.extract_audio,
        denoise_audio=options.denoise_audio,
        beautify=options.beautify,
        faststart=options.faststart,
    )
    return merged


def should_stream_copy(options: EditOptions) -> bool:
    return (
        not options.extract_audio
        and not options.video_filter
        and not options.crop
        and not options.resize
        and not options.overlay_text
        and not options.bg_music_track
        and not options.mute
        and not options.denoise_audio
        and not options.beautify
    )


def build_edit_command(
    input_file: str,
    output: Optional[str],
    options: EditOptions,
) -> List[str]:
    ffmpeg_path = require_binary("ffmpeg")
    input_path = Path(input_file)
    output_path = resolve_edit_output(input_path, output, options.extract_audio)
    command = [ffmpeg_path, "-y"]
    bg_music_path = resolve_bg_music_track(options.bg_music_track)
    if options.overlay_text and not ffmpeg_has_filter("drawtext"):
        print("Warning: drawtext filter is unavailable here, so running text will be skipped.", file=sys.stderr)

    if options.start:
        command.extend(["-ss", options.start])

    command.extend(["-i", str(input_path)])

    if bg_music_path:
        command.extend(["-stream_loop", "-1", "-i", str(bg_music_path)])

    if options.end:
        command.extend(["-to", options.end])
    elif options.duration:
        command.extend(["-t", options.duration])

    if options.extract_audio:
        command.extend(["-vn", "-acodec", "libmp3lame", "-q:a", "0", str(output_path)])
        return command

    if should_stream_copy(options):
        command.extend(["-c", "copy", str(output_path)])
        return command

    filters = build_video_filters(options)
    if filters:
        command.extend(["-vf", ",".join(filters)])

    if bg_music_path:
        music_volume = clamp_music_volume(options.bg_music_volume)
        has_original_audio = input_has_audio(input_path) and not options.mute
        if has_original_audio:
            audio_filters = build_audio_filters(options)
            original_chain = ",".join(audio_filters) if audio_filters else "anull"
            command.extend(
                [
                    "-filter_complex",
                    (
                        f"[0:a]{original_chain}[maina];"
                        f"[1:a]volume={music_volume:.2f}[bga];"
                        "[maina][bga]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                    ),
                    "-map",
                    "0:v:0",
                    "-map",
                    "[aout]",
                    "-shortest",
                ]
            )
        else:
            command.extend(
                [
                    "-filter_complex",
                    f"[1:a]volume={music_volume:.2f}[aout]",
                    "-map",
                    "0:v:0",
                    "-map",
                    "[aout]",
                    "-shortest",
                ]
            )
        command.extend(["-c:a", "aac"])
    elif options.mute:
        command.append("-an")
    else:
        audio_filters = build_audio_filters(options)
        if audio_filters:
            command.extend(["-af", ",".join(audio_filters)])
        command.extend(["-c:a", "aac"])

    command.extend(
        [
            "-c:v",
            options.video_codec,
            "-preset",
            options.preset,
            "-crf",
            str(options.crf),
        ]
    )
    if options.faststart and output_path.suffix.lower() in {".mp4", ".mov"}:
        command.extend(["-movflags", "+faststart"])
    command.append(str(output_path))
    return command


def run_command(command: List[str], print_only: bool) -> int:
    rendered = shlex.join(command)
    if print_only:
        print(rendered)
        return 0

    print(f"$ {rendered}")
    completed = subprocess.run(command, check=False, env=build_runtime_env())
    return completed.returncode


def run_download(args: argparse.Namespace) -> int:
    platform = detect_platform(args.url)
    if platform == "unknown":
        print(
            "Warning: URL platform not recognized. yt-dlp may still succeed if the site is supported.",
            file=sys.stderr,
        )
    elif platform not in SUPPORTED_SITES:
        print(
            f"Warning: {platform} is not in the primary support list {SUPPORTED_SITES}.",
            file=sys.stderr,
        )

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    command = build_download_command(args)
    return run_command(command, args.print_command)


def run_edit(args: argparse.Namespace) -> int:
    options = EditOptions(
        start=args.start,
        end=args.end,
        duration=args.duration,
        crop=args.crop,
        resize=args.resize,
        video_filter=args.video_filter,
        overlay_text=args.overlay_text,
        bg_music_track=args.bg_music_track,
        bg_music_volume=args.bg_music_volume,
        video_codec=args.video_codec,
        crf=args.crf,
        preset=args.encode_preset,
        mute=args.mute,
        extract_audio=args.extract_audio,
        denoise_audio=args.denoise_audio,
        beautify=args.beautify,
    )
    options = apply_edit_preset(options, args.preset_name)
    command = build_edit_command(args.input_file, args.output, options)
    return run_command(command, args.print_command)


def locate_latest_media(directory: str) -> Path:
    ignored_suffixes = {".json", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt", ".txt"}
    files = [
        path
        for path in Path(directory).rglob("*")
        if path.is_file()
        and not path.name.startswith(".")
        and path.suffix.lower() not in ignored_suffixes
    ]
    if not files:
        raise RuntimeError(f"No downloaded files found in {directory}")
    return max(files, key=lambda path: path.stat().st_mtime)


def run_workflow(args: argparse.Namespace) -> int:
    download_code = run_download(args)
    if download_code != 0 or args.audio_only:
        return download_code

    should_edit = any(
        [
            args.start,
            args.end,
            args.duration,
            args.preset_name != "none",
            args.crop,
            args.resize,
            args.video_filter,
            args.overlay_text,
            args.bg_music_track,
            args.mute,
            args.extract_audio,
            args.denoise_audio,
            args.beautify,
        ]
    )
    if not should_edit:
        return download_code

    latest_file = locate_latest_media(args.output_dir)
    options = EditOptions(
        start=args.start,
        end=args.end,
        duration=args.duration,
        crop=args.crop,
        resize=args.resize,
        video_filter=args.video_filter,
        overlay_text=args.overlay_text,
        bg_music_track=args.bg_music_track,
        bg_music_volume=args.bg_music_volume,
        video_codec=args.video_codec,
        crf=args.crf,
        preset=args.encode_preset,
        mute=args.mute,
        extract_audio=args.extract_audio,
        denoise_audio=args.denoise_audio,
        beautify=args.beautify,
    )
    options = apply_edit_preset(options, args.preset_name)
    command = build_edit_command(str(latest_file), None, options)
    return run_command(command, args.print_command)


def run_batch(args: argparse.Namespace) -> int:
    input_path = Path(args.input_file)
    if not input_path.exists():
        raise RuntimeError(f"URL list file not found: {input_path}")

    urls = [
        line.strip()
        for line in input_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not urls:
        raise RuntimeError("No valid URLs found in input file.")

    exit_code = 0
    for url in urls:
        item_args = argparse.Namespace(**vars(args))
        item_args.url = url
        exit_code = run_download(item_args)
        if exit_code != 0:
            return exit_code
    return exit_code


def inspect_environment(as_json: bool) -> int:
    report = {
        "python": sys.version.split()[0],
        "yt_dlp": require_binary("yt-dlp") if shutil.which("yt-dlp") or any(Path(p, "yt-dlp").exists() for p in FALLBACK_BIN_DIRS) else None,
        "ffmpeg": require_binary("ffmpeg") if shutil.which("ffmpeg") or any(Path(p, "ffmpeg").exists() for p in FALLBACK_BIN_DIRS) else None,
        "supported_platforms": list(SUPPORTED_SITES),
    }
    if as_json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"Python: {report['python']}")
    print(f"yt-dlp: {report['yt_dlp'] or 'missing'}")
    print(f"ffmpeg: {report['ffmpeg'] or 'missing'}")
    print(f"Supported platforms: {', '.join(report['supported_platforms'])}")
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    try:
        args = parse_args(argv)
        if args.command == "download":
            return run_download(args)
        if args.command == "edit":
            return run_edit(args)
        if args.command == "workflow":
            return run_workflow(args)
        if args.command == "batch":
            return run_batch(args)
        if args.command == "inspect":
            return inspect_environment(args.json)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
