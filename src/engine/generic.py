#!/usr/bin/env python3
# coding: utf-8

# ytdlbot - generic.py
# Ultra-optimized: subprocess yt-dlp (GIL-free) + Redis format caching + fast metadata

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from config import AUDIO_FORMAT
from utils import is_youtube, sizeof_fmt
from database.model import get_format_settings, get_quality_settings
from engine.base import BaseDownloader

logger = logging.getLogger(__name__)


def match_filter(info_dict):
    if info_dict.get("is_live"):
        raise NotImplementedError("Skipping live video")
    return None


class YoutubeDownload(BaseDownloader):

    _WORKER_SCRIPT = None  # lazy resolve

    @classmethod
    def _get_worker(cls) -> str:
        if cls._WORKER_SCRIPT is None:
            cls._WORKER_SCRIPT = os.path.normpath(
                os.path.join(os.path.dirname(__file__), "..", "worker.py")
            )
        return cls._WORKER_SCRIPT

    # ── builder helpers ──────────────────────────────────────────────

    @staticmethod
    def get_format(height: int) -> list[str]:
        return [
            f"bestvideo[ext=mp4][height={height}]+bestaudio[ext=m4a]",
            f"bestvideo[vcodec^=avc][height={height}]+bestaudio[acodec^=mp4a]/best[vcodec^=avc]/best",
        ]

    # ── format selection ──────────────────────────────────────────────

    def _setup_formats(self) -> list[str | None]:
        """Return the list of format specs that should be tried."""
        if not is_youtube(self._url):
            return [None]

        quality = get_quality_settings(self._chat_id)
        format_ = get_format_settings(self._chat_id)

        defaults: list[str | None] = [
            "bestvideo[ext=mp4][vcodec!*=av01][vcodec!*=vp09]+bestaudio[ext=m4a]/bestvideo+bestaudio",
            "bestvideo[vcodec^=avc]+bestaudio[acodec^=mp4a]/best[vcodec^=avc]/best",
            None,  # = yt-dlp auto
        ]
        audio_ext = AUDIO_FORMAT or "m4a"

        maps = {
            "high-audio":      [f"bestaudio[ext={audio_ext}]"],
            "high-video":      defaults,
            "high-document":   defaults,
            "medium-audio":    [f"bestaudio[ext={audio_ext}]"],
            "medium-video":    self.get_format(720),
            "medium-document": self.get_format(720),
            "low-audio":       [f"bestaudio[ext={audio_ext}]"],
            "low-video":       self.get_format(480),
            "low-document":    self.get_format(480),
            "custom-audio":    [""],  # TODO
            "custom-video":    [""],
            "custom-document": [""],
        }

        key = f"{quality}-{format_}"
        selected = maps.get(key, defaults)
        # append defaults as fallback (except for "high")
        if quality != "high":
            selected = list(selected) + list(defaults)
        return selected

    # ── Redis format cache ────────────────────────────────────────────

    def _format_cache_key(self) -> str:
        return f"ytdl_fmt_{self._url}"

    def _load_cached_formats(self) -> list[dict] | None:
        """Return cached format list or None."""
        try:
            from database.cache import Redis
            r = Redis().r
            raw = r.get(self._format_cache_key())
            if raw:
                return json.loads(raw)
        except Exception:
            pass
        return None

    def _save_cached_formats(self, formats: list[dict]) -> None:
        """Store format list in Redis with 1 hour TTL."""
        try:
            from database.cache import Redis
            r = Redis().r
            r.setex(self._format_cache_key(), 3600, json.dumps(formats))
        except Exception:
            pass

    # ── subprocess helpers ─────────────────────────────────────────────

    def _run_worker(self, task: dict) -> dict:
        """Call worker.py as subprocess and return decoded JSON."""
        proc = subprocess.run(
            [sys.executable, self._get_worker()],
            input=json.dumps(task),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"Worker exited with code {proc.returncode}")
        return json.loads(proc.stdout.strip())

    def _determine_cookiefile(self) -> str | None:
        if is_youtube(self._url):
            path = "youtube-cookies.txt"
            if os.path.isfile(path) and os.path.getsize(path) > 100:
                return os.path.abspath(path)
        return None

    # ── format pre-extraction (cached, subprocess) ────────────────────

    def _best_format_spec(self) -> str | None:
        """Extract video info (cached in Redis) and return the best format string."""
        cached = self._load_cached_formats()
        if cached:
            logger.info("Using cached format info for %s", self._url)
        else:
            logger.info("Extracting format info for %s via subprocess…", self._url)
            result = self._run_worker({
                "action": "extract",
                "url": self._url,
                "cookiefile": self._determine_cookiefile(),
            })
            if not result.get("success"):
                logger.warning("Extraction failed: %s", result.get("error"))
                return None
            cached = result.get("formats", [])
            self._save_cached_formats(cached)

        if not cached:
            return None

        # Strategy: prefer mp4 video + m4a audio
        mp4_only = [f for f in cached if f.get("ext") == "mp4" and f.get("vcodec") != "none"]
        m4a_only = [f for f in cached if f.get("ext") == "m4a" and f.get("acodec") != "none"]

        if mp4_only and m4a_only:
            return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio"
        # fallback to any video+audio
        return "bestvideo+bestaudio/best"

    # ── core download ─────────────────────────────────────────────────

    def _download(self, formats: list) -> list:
        """Try format specs in order via subprocess yt-dlp."""
        workdir = Path(self._tempdir.name)
        cookiefile = self._determine_cookiefile()

        # Build ordered trial list
        trial_formats: list[str | None] = []
        for f in formats:
            if f not in trial_formats:
                trial_formats.append(f)

        # Add dynamically detected best format (from Redis or fresh extract)
        best = self._best_format_spec()
        if best and best not in trial_formats:
            trial_formats.append(best)

        trial_formats.append("bestvideo+bestaudio/best")  # final fallback

        files = None
        for fmt in trial_formats:
            if not fmt:
                continue  # skip None/empty
            logger.info("Trying format: %s", fmt)
            try:
                result = self._run_worker({
                    "action": "download",
                    "url": self._url,
                    "format": fmt,
                    "output_dir": str(workdir),
                    "output_template": "%(title).70s.%(ext)s",
                    "cookiefile": cookiefile,
                })
                if result.get("success"):
                    files = result.get("files", [])
                    if files:
                        file_sizes = ", ".join(
                            sizeof_fmt(Path(f).stat().st_size) for f in files
                        )
                        logger.info("Download OK with format %s: %s (%s)", fmt, files, file_sizes)
                        break
                else:
                    logger.warning("Format %s failed: %s", fmt, result.get("error"))
            except Exception as e:
                logger.warning("Format %s raised: %s", fmt, e)

        return files

    # ── lifecycle ─────────────────────────────────────────────────────

    def _start(self, formats=None):
        default_formats = self._setup_formats()
        if formats is not None:
            default_formats = formats + (default_formats or [])
        files = self._download(default_formats)
        if files:
            self._upload(files)
        else:
            raise Exception("No files were downloaded. The video format may not be available.")