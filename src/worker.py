#!/usr/bin/env python3
# coding: utf-8

# ytdlbot - worker.py
# Subprocess worker for yt-dlp operations (GIL-free execution)

import json
import os
import subprocess
import sys
from pathlib import Path


def extract_info(url: str, cookiefile: str | None = None) -> dict:
    """Extract video info using yt-dlp subprocess. Returns JSON."""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--no-warnings", "--quiet",
        "--dump-json", "--no-download",
        "--extractor-args", "youtube:player-client=android_vr,tv,default,ios,web",
    ]
    if cookiefile and os.path.exists(cookiefile) and os.path.getsize(cookiefile) > 100:
        cmd.extend(["--cookies", cookiefile])
    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Extraction failed")

    lines = result.stdout.strip().split("\n")
    return json.loads(lines[-1])


def download_video(url: str, format_spec: str | None, output_dir: str,
                   output_template: str, cookiefile: str | None = None) -> list:
    """Download video using yt-dlp subprocess. Returns list of file paths."""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-playlist", "--no-warnings", "--quiet",
        "--restrict-filenames", "--windows-filenames",
        "--concurrent-fragments", "16",
        "--buffer-size", "4194304",
        "--retries", "6", "--fragment-retries", "6",
        "--skip-unavailable-fragments",
        "--embed-metadata", "--embed-thumbnail",
        "--no-write-thumbnail",
        "-o", os.path.join(output_dir, output_template),
        "--extractor-args", "youtube:player-client=android_vr,tv,default,ios,web",
    ]
    if cookiefile and os.path.exists(cookiefile) and os.path.getsize(cookiefile) > 100:
        cmd.extend(["--cookies", cookiefile])
    if format_spec:
        cmd.extend(["-f", format_spec])
    cmd.append(url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Download failed")

    return [str(f) for f in Path(output_dir).glob("*") if f.is_file()]


def main():
    """Worker entry point - receives JSON task via stdin, outputs JSON result via stdout."""
    input_line = sys.stdin.readline().strip()
    if not input_line:
        print(json.dumps({"success": False, "error": "No input"}))
        return

    task = json.loads(input_line)
    action = task.get("action", "download")

    try:
        if action == "extract":
            info = extract_info(task["url"], task.get("cookiefile"))
            formats = []
            if "formats" in info:
                for f in info["formats"]:
                    formats.append({
                        "id": f.get("format_id", ""),
                        "ext": f.get("ext", ""),
                        "height": f.get("height", 0),
                        "vcodec": f.get("vcodec", "none"),
                        "acodec": f.get("acodec", "none"),
                        "filesize": f.get("filesize", 0) or f.get("filesize_approx", 0),
                    })
            print(json.dumps({
                "success": True,
                "title": info.get("title", ""),
                "duration": info.get("duration", 0),
                "formats": formats,
                "thumbnail": info.get("thumbnail", ""),
            }))

        elif action == "download":
            files = download_video(
                task["url"], task.get("format"),
                task["output_dir"], task.get("output_template", "%(title).70s.%(ext)s"),
                task.get("cookiefile"),
            )
            print(json.dumps({"success": True, "files": files}))

        else:
            print(json.dumps({"success": False, "error": f"Unknown action: {action}"}))

    except subprocess.TimeoutExpired:
        print(json.dumps({"success": False, "error": "Operation timed out"}))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}))


if __name__ == "__main__":
    main()