#!/usr/bin/env python3
# coding: utf-8

# ytdlbot - base.py
# Optimized: no ffprobe metadata scanning, reduced API calls, fast path

import asyncio
import hashlib
import json
import logging
import re
import tempfile
from abc import ABC, abstractmethod
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import final

import filetype
from pyrogram import enums, types
from tqdm import tqdm

from config import TG_NORMAL_MAX_SIZE, Types
from database import Redis
from database.model import (
    check_quota,
    get_format_settings,
    get_free_quota,
    get_paid_quota,
    get_quality_settings,
    use_quota,
)
from engine.helper import debounce, sizeof_fmt

logger = logging.getLogger(__name__)


def run_sync(client, coro):
    if asyncio.iscoroutine(coro):
        future = asyncio.run_coroutine_threadsafe(coro, client.loop)
        return future.result()
    return coro


def generate_input_media(file_paths: list, cap: str) -> list:
    input_media = []
    for path in file_paths:
        mime = filetype.guess_mime(path)
        if "video" in mime:
            input_media.append(types.InputMediaVideo(media=path))
        elif "image" in mime:
            input_media.append(types.InputMediaPhoto(media=path))
        elif "audio" in mime:
            input_media.append(types.InputMediaAudio(media=path))
        else:
            input_media.append(types.InputMediaDocument(media=path))
    if input_media:
        input_media[0].caption = cap
    return input_media


class BaseDownloader(ABC):
    def __init__(self, client: Types.Client, bot_msg: types.Message, url: str):
        self._client = client
        self._url = url
        self._chat_id = self._from_user = bot_msg.chat.id
        if bot_msg.chat.type in (enums.ChatType.GROUP, enums.ChatType.SUPERGROUP):
            self._from_user = bot_msg.reply_to_message.from_user.id
        self._id = bot_msg.id
        self._tempdir = tempfile.TemporaryDirectory(prefix="ytdl-")
        self._bot_msg: types.Message = bot_msg
        self._redis = Redis()
        self._quality = get_quality_settings(self._chat_id)
        self._format = get_format_settings(self._chat_id)

    def __del__(self):
        try:
            self._tempdir.cleanup()
        except Exception:
            pass

    def _record_usage(self):
        free, paid = get_free_quota(self._from_user), get_paid_quota(self._from_user)
        logger.info("User %s has %s free and %s paid quota", self._from_user, free, paid)
        if free + paid < 0:
            raise Exception("Usage limit exceeded")
        use_quota(self._from_user)

    # ── progress helpers (minimized API calls) ────────────────────────

    @staticmethod
    def _render_progress(desc: str, total: int, finished: int, speed="", eta="") -> str:
        """Render a compact progress bar string."""
        if total <= 0:
            return f"`{desc}` `{sizeof_fmt(finished)}`"

        f = StringIO()
        tqdm(
            total=total,
            initial=finished,
            file=f,
            ascii=False,
            unit_scale=True,
            ncols=20,
            bar_format="{l_bar}{bar}|",
        )
        raw = f.getvalue()
        parts = raw.split("|")
        bar = parts[1] if len(parts) > 1 else ""
        pct = int(finished / total * 100) if total else 0
        f.close()

        txt = f"`{desc}` `{pct}%` `{bar}` `{sizeof_fmt(finished)}/{sizeof_fmt(total)}`"
        if speed:
            txt += f" `{speed}`"
        if eta:
            txt += f" `ETA:{eta}`"
        return txt

    def download_hook(self, d: dict):
        """Called from old Python yt-dlp API (kept for compat, not used by subprocess)."""
        pass

    def upload_hook(self, current, total):
        """Called during Telegram upload."""
        text = self._render_progress("Uploading…", total, current)
        self.edit_text(text)

    # Progress bar update every 10 seconds (reduce API calls)
    @debounce(10)
    def edit_text(self, text: str):
        run_sync(self._client, self._bot_msg.edit_text(text))

    # ── abstract ──────────────────────────────────────────────────────

    @abstractmethod
    def _setup_formats(self):
        pass

    @abstractmethod
    def _download(self, formats) -> list:
        pass

    # ── upload ────────────────────────────────────────────────────────

    @property
    def _methods(self):
        return {
            "document": self._client.send_document,
            "audio": self._client.send_audio,
            "video": self._client.send_video,
            "animation": self._client.send_animation,
            "photo": self._client.send_photo,
        }

    def send_something(self, *, chat_id, files, _type, caption=None, thumb=None, **kwargs):
        run_sync(self._client, self._client.send_chat_action(chat_id, enums.ChatAction.UPLOAD_DOCUMENT))
        is_cache = kwargs.pop("cache", False)

        if len(files) > 1 and not is_cache:
            inputs = generate_input_media(files, caption)
            return run_sync(self._client, self._client.send_media_group(chat_id, inputs))[0]

        file_arg_map = {
            "photo": "photo", "video": "video",
            "animation": "animation", "document": "document", "audio": "audio",
        }
        file_arg_name = file_arg_map.get(_type)
        if not file_arg_name:
            logger.error("Unknown _type: %s", _type)
            return None

        send_args = {
            "chat_id": chat_id,
            file_arg_name: files[0],
            "caption": caption,
            "progress": self.upload_hook,
            **kwargs,
        }
        if _type in ("video", "animation", "document", "audio") and thumb is not None:
            send_args["thumb"] = thumb

        return run_sync(self._client, self._methods[_type](**send_args))

    def get_metadata(self) -> dict:
        """Fast metadata extraction — skip ffprobe, use file stat for size."""
        video_files = list(Path(self._tempdir.name).glob("*"))
        if not video_files:
            return {"height": 0, "width": 0, "duration": 0, "thumb": None, "caption": self._url}

        video_path = video_files[0]
        filename = Path(video_path).name
        try:
            size = video_path.stat().st_size
        except OSError:
            size = 0

        caption = (
            f"{self._url}\n{filename}\n\n"
            f"Size: {sizeof_fmt(size)}"
        )
        return {
            "height": 0, "width": 0, "duration": 0,
            "thumb": None, "caption": caption,
        }

    def _upload(self, files=None, meta=None):
        if files is None:
            files = list(Path(self._tempdir.name).glob("*"))
        if meta is None:
            meta = self.get_metadata()

        success = SimpleNamespace(document=None, video=None, audio=None, animation=None, photo=None)

        if self._format == "document":
            logger.info("Sending as document for %s", self._url)
            success = self.send_something(
                chat_id=self._chat_id, files=files, _type="document",
                thumb=meta.get("thumb"), force_document=True,
                caption=meta.get("caption"),
            )
        elif self._format == "photo":
            logger.info("Sending as photo for %s", self._url)
            success = self.send_something(
                chat_id=self._chat_id, files=files, _type="photo",
                caption=meta.get("caption"),
            )
        elif self._format == "audio":
            logger.info("Sending as audio for %s", self._url)
            success = self.send_something(
                chat_id=self._chat_id, files=files, _type="audio",
                caption=meta.get("caption"),
            )
        elif self._format == "video":
            logger.info("Sending as video for %s", self._url)
            for method in ("video", "animation", "audio", "photo"):
                try:
                    meta_copy = dict(meta)
                    if method == "photo":
                        meta_copy.pop("thumb", None)
                        meta_copy.pop("duration", None)
                        meta_copy.pop("height", None)
                        meta_copy.pop("width", None)
                    elif method == "audio":
                        meta_copy.pop("height", None)
                        meta_copy.pop("width", None)
                    success = self.send_something(
                        chat_id=self._chat_id, files=files,
                        _type=method, **meta_copy,
                    )
                    break
                except Exception as e:
                    logger.warning("Send as %s failed: %s", method, e)
            else:
                raise ValueError("ERROR: For direct links, try again with `/direct`.")
        else:
            logger.error("Unknown format %s", self._format)
            return

        # Cache result
        video_key = self._calc_video_key()
        obj = success.document or success.video or success.audio or success.animation or success.photo
        mapping = {
            "file_id": json.dumps([getattr(obj, "file_id", None)]),
            "meta": json.dumps({k: v for k, v in meta.items() if k != "thumb"}, ensure_ascii=False),
        }
        self._redis.add_cache(video_key, mapping)
        run_sync(self._client, self._bot_msg.edit_text("✅ Success"))
        return success

    # ── cache ─────────────────────────────────────────────────────────

    def _get_video_cache(self):
        return self._redis.get_cache(self._calc_video_key())

    def _calc_video_key(self) -> str:
        h = hashlib.md5()
        h.update((self._url + self._quality + self._format).encode())
        return h.hexdigest()

    # ── entry ─────────────────────────────────────────────────────────

    @final
    def start(self):
        check_quota(self._from_user)
        if cache := self._get_video_cache():
            logger.info("Cache hit for %s", self._url)
            meta, file_id = json.loads(cache["meta"]), json.loads(cache["file_id"])
            meta["cache"] = True
            self._upload(file_id, meta)
        else:
            self._start()
        self._record_usage()

    @abstractmethod
    def _start(self):
        pass