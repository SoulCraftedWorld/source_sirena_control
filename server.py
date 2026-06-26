from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
import zlib

from ego_log import FRAME_HEADER, FRAME_MAGIC, GPS_FIX, TYPE_GPS_FIX, EgoLogWriter
from inputs import AudioCapture, NmeaService, TriggerService


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = ROOT / "session_history.json"
COUNTER_PATH = ROOT / "session_counter.json"
LOG_NAME_RE = re.compile(r"^SRC[1-3]_S[A-Za-z0-9_.-]+\.bin$")
MAX_FRAME_PAYLOAD = 256 * 1024 * 1024

TEST_CATALOG = [
    {"group": "LAB", "id": "LAB-01", "name": "Сирена, тип 1"},
    {"group": "LAB", "id": "LAB-02", "name": "Сирена, тип 2"},
    {"group": "LAB", "id": "LAB-03", "name": "Сирена, произвольный тип"},
    {"group": "LAB", "id": "LAB-04", "name": "Калибровка SPL, 2 м"},
    {"group": "CAL", "id": "CAL-01", "name": "Собственный шум, стоянка"},
    {"group": "CAL", "id": "CAL-02", "name": "Шум движения без сирены"},
    {"group": "CAL", "id": "CAL-03", "name": "Ветер и остаточные возмущения"},
    {"group": "FT-S", "id": "FT-S-FRONT", "name": "Статика, источник спереди"},
    {"group": "FT-S", "id": "FT-S-REAR", "name": "Статика, источник сзади"},
    {"group": "FT-S", "id": "FT-S-SIDE", "name": "Статика, источник сбоку"},
    {"group": "FT-S", "id": "FT-S-DIAG", "name": "Статика, диагональное направление"},
    {"group": "FT-D", "id": "FT-D1", "name": "Встречное движение"},
    {"group": "FT-D", "id": "FT-D2", "name": "Эго догоняет источник"},
    {"group": "FT-D", "id": "FT-D3", "name": "Источник догоняет Эго"},
    {"group": "FT-D", "id": "FT-D4", "name": "Перекрёсток 90°"},
    {"group": "FT-D", "id": "FT-D5", "name": "Параллельный проезд"},
    {"group": "FT-D6", "id": "FT-D6.1", "name": "Равномерное движение 50 км/ч"},
    {"group": "FT-D6", "id": "FT-D6.2", "name": "Плавный разгон 0–80 км/ч"},
    {"group": "FT-D6", "id": "FT-D6.3", "name": "Торможение 80–0 км/ч"},
    {"group": "FT-D7", "id": "FT-D7.1", "name": "Попутный автомобиль рядом"},
    {"group": "FT-D7", "id": "FT-D7.2", "name": "Встречный автомобиль"},
    {"group": "FT-D7", "id": "FT-D7.3", "name": "Два попутных автомобиля"},
    {"group": "FT-D8", "id": "FT-D8.1", "name": "Слабый дождь"},
    {"group": "FT-D8", "id": "FT-D8.2", "name": "Сильный дождь"},
    {"group": "FT-D8", "id": "FT-D8.3", "name": "Встречный ветер"},
    {"group": "FT-D8", "id": "FT-D8.4", "name": "Попутный ветер"},
    {"group": "FT-D8", "id": "FT-D8.5", "name": "После дождя"},
    {"group": "FT-D9", "id": "FT-D9-A", "name": "Базовая схема микрофонов"},
    {"group": "FT-D9", "id": "FT-D9-B", "name": "Сокращённая схема микрофонов"},
    {"group": "FT-Multi", "id": "FT-Multi.02", "name": "Два источника спереди"},
    {"group": "FT-Multi", "id": "FT-Multi.03", "name": "Источник спереди и сзади"},
    {"group": "FT-Multi", "id": "FT-Multi.04", "name": "Источники слева и справа"},
    {"group": "FT-N", "id": "FT-N.01", "name": "Поток без сирены"},
    {"group": "FT-N", "id": "FT-N.02", "name": "Мотоцикл без сирены"},
    {"group": "FT-N", "id": "FT-N.04", "name": "Городской гудок"},
    {"group": "FT-N", "id": "FT-N.05", "name": "Шум мокрой дороги"},
    {"group": "FT-N", "id": "FT-N.06", "name": "Тоннель или эстакада"},
    {"group": "FT-N", "id": "FT-N.07", "name": "Городской шум на стоянке"},
    {"group": "FT-N", "id": "FT-N.08", "name": "Трасса 80 км/ч без сирены"},
    {"group": "CUSTOM", "id": "CUSTOM", "name": "Пользовательское испытание"},
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def increment_session_number(value: str) -> str:
    text = str(value or "").strip()
    match = re.match(r"^(.*?)(\d+)$", text)
    if match is None:
        return text
    prefix, digits = match.groups()
    return f"{prefix}{int(digits) + 1:0{len(digits)}d}"


def atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path) -> dict[str, Any]:
    example = json.loads(
        (ROOT / "config.example.json").read_text(encoding="utf-8")
    )
    if path.is_file():
        config = deep_merge(
            example, json.loads(path.read_text(encoding="utf-8"))
        )
    else:
        config = example
        atomic_json(path, config)
    source_id = int(config["source_id"])
    if source_id < 1 or source_id > 3:
        raise ValueError("source_id must be 1..3")
    return config


class RingLogHandler(logging.Handler):
    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.lines: deque[dict[str, Any]] = deque(maxlen=capacity)
        self.lock = threading.RLock()
        self.sequence = 0

    def emit(self, record: logging.LogRecord) -> None:
        with self.lock:
            self.lines.append({
                "sequence": self.sequence,
                "time": datetime.now().strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": self.format(record),
            })
            self.sequence += 1

    def state(self, after: int | None = None) -> dict[str, Any]:
        with self.lock:
            lines = list(self.lines)
            if after is not None:
                lines = [line for line in lines if line["sequence"] > after]
            return {"lines": lines, "next_sequence": self.sequence}

    def clear(self) -> None:
        with self.lock:
            self.lines.clear()


class S3Uploader:
    def __init__(self, app: "SourceApplication") -> None:
        self.app = app
        self.records: dict[str, dict[str, Any]] = {}
        self.queue: queue.Queue[str] = queue.Queue()
        self.queued: set[str] = set()
        self.cancelled: set[str] = set()
        self.lock = threading.RLock()
        self.worker = threading.Thread(
            target=self._run, name="source-s3", daemon=True
        )
        self.worker.start()

    def _settings(self) -> dict[str, Any]:
        settings = dict(self.app.config["s3"])
        settings["secret_access_key"] = os.environ.get(
            "SOURCE_SIRENA_S3_SECRET_ACCESS_KEY",
            self.app.runtime_s3_secret,
        )
        settings["session_token"] = os.environ.get(
            "SOURCE_SIRENA_S3_SESSION_TOKEN",
            self.app.runtime_s3_token,
        )
        return settings

    @staticmethod
    def _client(settings: dict[str, Any]):
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError("boto3 is not installed") from exc
        kwargs = {
            "service_name": "s3",
            "region_name": settings.get("region") or "us-east-1",
            "aws_access_key_id": settings.get("access_key_id"),
            "aws_secret_access_key": settings.get("secret_access_key"),
        }
        if settings.get("endpoint_url"):
            kwargs["endpoint_url"] = settings["endpoint_url"]
        if settings.get("session_token"):
            kwargs["aws_session_token"] = settings["session_token"]
        return boto3.client(**kwargs)

    def public_settings(self) -> dict[str, Any]:
        settings = self._settings()
        return {
            key: settings.get(key, "")
            for key in (
                "endpoint_url", "bucket", "region", "access_key_id",
                "prefix", "auto_upload",
            )
        } | {
            "secret_configured": bool(settings.get("secret_access_key")),
            "session_token_configured": bool(settings.get("session_token")),
            "ready": bool(
                settings.get("bucket")
                and settings.get("access_key_id")
                and settings.get("secret_access_key")
            ),
        }

    def configure(self, raw: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "endpoint_url", "bucket", "region", "access_key_id",
            "prefix", "auto_upload",
        }
        for key in allowed:
            if key in raw:
                self.app.config["s3"][key] = (
                    bool(raw[key]) if key == "auto_upload"
                    else str(raw[key]).strip()
                )
        if raw.get("secret_access_key"):
            self.app.runtime_s3_secret = str(raw["secret_access_key"])
        if raw.get("session_token"):
            self.app.runtime_s3_token = str(raw["session_token"])
        self.app.save_config()
        return self.public_settings()

    def test(self) -> dict[str, Any]:
        settings = self._settings()
        self._client(settings).head_bucket(Bucket=settings["bucket"])
        return {"ok": True, "bucket": settings["bucket"]}

    def enqueue(self, name: str) -> None:
        self.app.log_path(name)
        with self.lock:
            if name in self.queued:
                return
            self.queued.add(name)
            self.records[name] = {
                "status": "upload_queued", "progress_bytes": 0, "error": "",
            }
        self.queue.put(name)

    def cancel(self, name: str) -> bool:
        with self.lock:
            if name not in self.queued:
                return False
            self.cancelled.add(name)
            self.records[name]["status"] = "canceling"
            return True

    def _run(self) -> None:
        while True:
            name = self.queue.get()
            try:
                self._upload(name)
            except Exception as exc:
                with self.lock:
                    canceled = name in self.cancelled
                    self.records[name].update(
                        status="canceled" if canceled else "upload_error",
                        error="" if canceled else str(exc),
                    )
                if not canceled:
                    logging.exception("S3 upload failed for %s", name)
            finally:
                with self.lock:
                    self.queued.discard(name)
                    self.cancelled.discard(name)
                self.queue.task_done()

    def _upload(self, name: str) -> None:
        path = self.app.log_path(name)
        settings = self._settings()
        prefix = str(settings.get("prefix", "")).strip("/")
        key = f"{prefix}/{name}" if prefix else name
        with self.lock:
            self.records[name] = {
                "status": "uploading", "progress_bytes": 0,
                "error": "", "s3_key": key,
            }

        def progress(size: int) -> None:
            with self.lock:
                if name in self.cancelled:
                    raise RuntimeError("upload canceled")
                self.records[name]["progress_bytes"] += size

        client = self._client(settings)
        client.upload_file(
            str(path), settings["bucket"], key,
            ExtraArgs={"ContentType": "application/octet-stream"},
            Callback=progress,
        )
        response = client.head_object(Bucket=settings["bucket"], Key=key)
        with self.lock:
            self.records[name].update(
                status="uploaded", s3_etag=str(response.get("ETag", "")).strip('"')
            )

    def record(self, name: str) -> dict[str, Any]:
        with self.lock:
            return dict(self.records.get(name, {}))


class LocalPcSender:
    def __init__(self, app: "SourceApplication") -> None:
        self.app = app
        self.queue: queue.Queue[str] = queue.Queue()
        self.queued: set[str] = set()
        self.records: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = threading.Thread(
            target=self._run, name="source-localpc", daemon=True
        )
        self.worker.start()

    def settings(self) -> dict[str, Any]:
        return dict(self.app.config.get("localpc", {}))

    def enqueue(self, name: str, force: bool = False) -> bool:
        self.app.log_path(name)
        if not force and not self.settings().get("enabled", False):
            return False
        with self.lock:
            if name in self.queued:
                return True
            self.queued.add(name)
            self.records[name] = {
                "status": "queued",
                "progress_bytes": 0,
                "error": "",
            }
        self.queue.put(name)
        return True

    def record(self, name: str) -> dict[str, Any]:
        with self.lock:
            return dict(self.records.get(name, {}))

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                name = self.queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._send_with_retry(name)
            finally:
                with self.lock:
                    self.queued.discard(name)
                self.queue.task_done()

    def _send_with_retry(self, name: str) -> None:
        settings = self.settings()
        deadline = time.monotonic() + float(settings.get("retry_window_s", 20.0))
        interval = max(0.2, float(settings.get("retry_interval_s", 2.0)))
        last_error = ""
        while not self.stop_event.is_set():
            try:
                self._send_once(name, settings)
                with self.lock:
                    self.records[name].update(status="sent", error="")
                logging.info("LocalPC push completed for %s", name)
                return
            except Exception as exc:
                last_error = str(exc)
                with self.lock:
                    self.records[name].update(status="retrying", error=last_error)
                logging.warning("LocalPC push failed for %s: %s", name, exc)
                if time.monotonic() + interval > deadline:
                    break
                self.stop_event.wait(interval)
        with self.lock:
            self.records[name].update(status="error", error=last_error)

    def _send_once(self, name: str, settings: dict[str, Any]) -> None:
        path = self.app.log_path(name)
        size = path.stat().st_size
        host = str(settings.get("host", ""))
        port = int(settings.get("port", 10201))
        if not host:
            raise RuntimeError("LocalPC host is not configured")
        timeout = float(settings.get("connect_timeout_s", 3.0))
        correlation_key = ""
        for item in reversed(self.app.history):
            if item.get("log_name") == name:
                correlation_key = str(item.get("correlation_key", ""))
                break
        header = {
            "protocol": "ego-source-log/1",
            "name": name,
            "size": size,
            "source_id": int(self.app.config["source_id"]),
            "source_name": self.app.config["source_name"],
            "correlation_key": correlation_key,
        }
        with self.lock:
            self.records[name].update(
                status="sending",
                progress_bytes=0,
                error="",
            )
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(
                json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n"
            )
            sent = 0
            with path.open("rb") as input_file:
                while True:
                    chunk = input_file.read(1024 * 256)
                    if not chunk:
                        break
                    sock.sendall(chunk)
                    sent += len(chunk)
                    with self.lock:
                        self.records[name]["progress_bytes"] = sent
            response = sock.recv(256)
        if not response.startswith(b"OK "):
            raise RuntimeError(
                response.decode("utf-8", errors="replace").strip()
                or "LocalPC receiver rejected log"
            )

    def close(self) -> None:
        self.stop_event.set()
        self.worker.join(timeout=2.0)


class EgoSyncClient:
    def __init__(self, app: "SourceApplication") -> None:
        self.app = app
        self.stop_event = threading.Event()
        self.worker = threading.Thread(
            target=self._run, name="source-ego-sync", daemon=True
        )
        self.worker.start()

    def _settings(self) -> dict[str, Any]:
        return dict(self.app.config.get("ego_sync", {}))

    def _url(self) -> str:
        settings = self._settings()
        base_url = str(settings.get("base_url") or "").strip().rstrip("/")
        if base_url:
            return base_url
        host = str(
            settings.get("host")
            or self.app.config.get("localpc", {}).get("host")
            or ""
        ).strip()
        if not host:
            return ""
        if host.startswith("http://") or host.startswith("https://"):
            return host.rstrip("/")
        port = int(settings.get("web_port") or 80)
        suffix = "" if port == 80 else f":{port}"
        return f"http://{host}{suffix}"

    @staticmethod
    def _post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _run(self) -> None:
        while not self.stop_event.is_set():
            settings = self._settings()
            timeout = float(settings.get("timeout_s") or 2.0)
            interval = min(1.0, float(settings.get("poll_interval_s") or 1.0))
            base_url = self._url()
            try:
                if not base_url:
                    raise RuntimeError("EGO host is not configured")
                response = self._post_json(
                    f"{base_url}/api/source-sync/poll",
                    self.app.ego_sync_poll_payload(),
                    timeout,
                )
                desired = response.get("desired")
                if desired and self.app.ego_sync_enabled():
                    self.app.apply_ego_sync(desired)
                self.app.update_ego_sync_status(
                    available=True,
                    error="",
                    version=int(response.get("version") or 0),
                )
            except (OSError, HTTPError, URLError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                self.app.update_ego_sync_status(
                    available=False, error=str(exc), version=None
                )
            self.stop_event.wait(max(0.2, interval))

    def close(self) -> None:
        self.stop_event.set()
        self.worker.join(timeout=2.0)


class SourceApplication:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = load_config(config_path)
        self.runtime_s3_secret = ""
        self.runtime_s3_token = ""
        self.lock = threading.RLock()
        self.writer: EgoLogWriter | None = None
        self.active: dict[str, Any] | None = None
        self.audio: AudioCapture | None = None
        self.ego_sync_local_fields: dict[str, Any] = {}
        self.ego_sync_applied_fields: dict[str, Any] = {}
        self.ego_sync_applied_version = 0
        self.ego_sync_status: dict[str, Any] = {
            "available": False,
            "last_ok_utc": "",
            "last_error": "",
            "status": "ожидание связи с EGO",
        }
        self.history = self._load_history()
        self.uploader = S3Uploader(self)
        self.localpc = LocalPcSender(self)
        self.ego_sync = EgoSyncClient(self)
        self.nmea: NmeaService
        self.trigger: TriggerService
        self._start_inputs()

    def _load_history(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_history(self) -> None:
        atomic_json(HISTORY_PATH, self.history[-100:])

    def _load_counter(self) -> str:
        try:
            value = json.loads(COUNTER_PATH.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return str(value.get("next_session_number", "")).strip()
        except (OSError, json.JSONDecodeError):
            pass
        return ""

    def _save_counter(self, next_session_number: str) -> None:
        atomic_json(
            COUNTER_PATH,
            {"next_session_number": str(next_session_number).strip()},
        )

    @staticmethod
    def _next_from_history(history: list[dict[str, Any]]) -> str:
        highest = 0
        width = 1
        for item in history:
            text = str(item.get("session_number", "")).strip()
            if not text.isdigit():
                continue
            highest = max(highest, int(text))
            width = max(width, len(text))
        return f"{highest + 1:0{width}d}" if highest else "1"

    def _next_session_number(self) -> str:
        return self._load_counter() or self._next_from_history(self.history)

    def _advance_counter(self, session_number: str) -> None:
        next_number = increment_session_number(session_number)
        if next_number and next_number != session_number:
            self._save_counter(next_number)

    def save_config(self) -> None:
        atomic_json(self.config_path, self.config)

    @staticmethod
    def _sync_fields(raw: dict[str, Any]) -> dict[str, Any]:
        def clean(key: str, limit: int = 96) -> str:
            return str(raw.get(key, "") or "").strip()[:limit]

        try:
            repeat_number = int(raw.get("repeat_number") or 0)
        except (TypeError, ValueError):
            repeat_number = 0
        return {
            "session_number": clean("session_number", 24),
            "repeat_number": repeat_number,
            "test_group": clean("test_group", 32),
            "test_id": clean("test_id", 32),
            "test_name": clean("test_name", 96),
            "siren_type": clean("siren_type", 48),
        }

    def ego_sync_enabled(self) -> bool:
        return bool(self.config.get("ego_sync", {}).get("enabled", True))

    def ego_start_with_ego_enabled(self) -> bool:
        return bool(self.config.get("ego_sync", {}).get("start_with_ego", True))

    def update_ego_sync_local_fields(self, raw: dict[str, Any]) -> dict[str, Any]:
        fields = self._sync_fields(raw)
        with self.lock:
            self.ego_sync_local_fields = fields
        return self.ego_sync_state()

    def ego_sync_poll_payload(self) -> dict[str, Any]:
        with self.lock:
            local_fields = dict(self.ego_sync_local_fields)
            applied_fields = dict(self.ego_sync_applied_fields)
            siren_type = (
                local_fields.get("siren_type")
                or applied_fields.get("siren_type")
                or ""
            )
            return {
                "source_id": int(self.config["source_id"]),
                "source_name": self.config.get("source_name", ""),
                "sync_enabled": self.ego_sync_enabled(),
                "start_with_ego": self.ego_start_with_ego_enabled(),
                "siren_type": siren_type,
                "version": self.ego_sync_applied_version,
                "applied_version": self.ego_sync_applied_version,
                "status": self.ego_sync_status.get("status", ""),
            }

    def apply_ego_sync(self, raw: dict[str, Any]) -> None:
        fields = self._sync_fields(raw)
        version = int(raw.get("version") or 0)
        command = str(raw.get("command") or "set").strip().lower()
        should_start = False
        should_stop = False
        with self.lock:
            recording = bool(self.writer)
            if recording and command != "stop":
                self.ego_sync_status.update(
                    status="EGO sync received during recording; deferred"
                )
                return
            self.ego_sync_applied_fields = fields
            self.ego_sync_applied_version = version
            if fields.get("session_number"):
                self._save_counter(str(fields["session_number"]))
            should_start = (
                command == "start"
                and self.ego_sync_enabled()
                and self.ego_start_with_ego_enabled()
                and not recording
            )
            should_stop = command == "stop" and recording
            self.ego_sync_status.update(
                status="задано",
                last_applied_utc=utc_now(),
            )

        if should_start:
            try:
                self.start_session(fields)
                with self.lock:
                    self.ego_sync_status["status"] = "запущено по EGO"
            except Exception as exc:
                logging.exception("EGO synchronized start failed")
                with self.lock:
                    self.ego_sync_status["status"] = "ошибка запуска по EGO"
                    self.ego_sync_status["last_error"] = str(exc)
        elif should_stop:
            try:
                self.stop_session()
                with self.lock:
                    self.ego_sync_status["status"] = "остановлено по EGO"
            except Exception as exc:
                logging.exception("EGO synchronized stop failed")
                with self.lock:
                    self.ego_sync_status["status"] = "ошибка остановки по EGO"
                    self.ego_sync_status["last_error"] = str(exc)

    def update_ego_sync_status(
        self, available: bool, error: str, version: int | None = None
    ) -> None:
        with self.lock:
            self.ego_sync_status["available"] = available
            self.ego_sync_status["last_error"] = error
            if available:
                self.ego_sync_status["last_ok_utc"] = utc_now()
                if version is not None:
                    self.ego_sync_status["ego_version"] = version
                if self.ego_sync_status.get("status") in {"", "EGO недоступен"}:
                    self.ego_sync_status["status"] = "связь есть"
            else:
                self.ego_sync_status["status"] = "EGO недоступен"

    def set_ego_sync_enabled(self, enabled: bool) -> dict[str, Any]:
        with self.lock:
            self.config.setdefault("ego_sync", {})["enabled"] = bool(enabled)
            self.save_config()
        return self.ego_sync_state()

    def set_ego_sync_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            config = self.config.setdefault("ego_sync", {})
            if "enabled" in raw:
                config["enabled"] = bool(raw.get("enabled"))
            if "start_with_ego" in raw:
                config["start_with_ego"] = bool(raw.get("start_with_ego"))
            self.save_config()
        return self.ego_sync_state()

    def ego_sync_state(self) -> dict[str, Any]:
        with self.lock:
            return {
                "enabled": self.ego_sync_enabled(),
                "start_with_ego": self.ego_start_with_ego_enabled(),
                "status": dict(self.ego_sync_status),
                "local_fields": dict(self.ego_sync_local_fields),
                "applied_fields": dict(self.ego_sync_applied_fields),
                "applied_version": self.ego_sync_applied_version,
                "settings": dict(self.config.get("ego_sync", {})),
            }

    @property
    def logs_dir(self) -> Path:
        path = Path(self.config["storage"]["logs_dir"]).expanduser()
        if not path.is_absolute():
            path = (ROOT / path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_path(self, name: str) -> Path:
        if LOG_NAME_RE.fullmatch(name) is None:
            raise ValueError("invalid log name")
        path = (self.logs_dir / name).resolve()
        if path.parent != self.logs_dir:
            raise ValueError("invalid log path")
        return path

    def _start_inputs(self) -> None:
        self.nmea = NmeaService(self.config["nmea"], self._on_gps)
        self.trigger = TriggerService(
            self.config["siren_trigger"], self._on_trigger
        )
        self.nmea.start()
        self.trigger.start()

    def _stop_inputs(self) -> None:
        self.nmea.close()
        self.trigger.close()

    def _on_gps(self, fix: dict[str, Any]) -> None:
        with self.lock:
            if self.writer:
                self.writer.write_gps(fix)

    def _on_trigger(self, active: bool) -> None:
        with self.lock:
            if self.writer:
                self.writer.write_trigger(active, int(self.config["source_id"]))

    def _on_audio(
        self, data: bytes, rate: int, channels: int, sample_bytes: int, frames: int
    ) -> None:
        with self.lock:
            if self.writer:
                self.writer.write_audio(data, rate, channels, sample_bytes, frames)

    @staticmethod
    def _clean(value: Any, limit: int) -> str:
        return str(value or "").strip()[:limit]

    def session_catalog(self) -> dict[str, Any]:
        numbers = sorted(
            {
                str(item.get("session_number", "")).strip()
                for item in self.history
                if str(item.get("session_number", "")).strip()
            },
            key=lambda value: (
                not value.isdigit(),
                int(value) if value.isdigit() else value,
            ),
        )
        return {
            "tests": TEST_CATALOG,
            "session_numbers": numbers,
            "next_session_number": self._next_session_number(),
        }

    def start_session(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.writer:
                raise RuntimeError("session already active")
            source_id = int(self.config["source_id"])
            session_number = self._clean(raw.get("session_number"), 24)
            test_id = self._clean(raw.get("test_id"), 32)
            known = next(
                (item for item in TEST_CATALOG if item["id"] == test_id), None
            )
            if known is None:
                raise ValueError("unknown test type")
            if not session_number:
                raise ValueError("session number is required")
            repeat = int(raw.get("repeat_number", 1))
            if repeat < 1:
                raise ValueError("repeat number must be positive")
            correlation = f"S{session_number}_{test_id}_R{repeat}"
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            name = f"SRC{source_id}_{correlation}_{timestamp}.bin"
            metadata = {
                "session_number": session_number,
                "test_group": known["group"],
                "test_id": test_id,
                "test_name": self._clean(
                    raw.get("test_name") or known["name"], 96
                ),
                "custom_name": self._clean(raw.get("custom_name"), 96),
                "repeat_number": repeat,
                "siren_type": self._clean(raw.get("siren_type"), 48),
                "operator": self._clean(raw.get("operator"), 64),
                "comment": self._clean(raw.get("comment"), 320),
                "source_id": source_id,
                "source_name": self.config["source_name"],
                "source_role": "siren_source",
                "correlation_key": correlation,
                "started_utc": utc_now(),
                "log_name": name,
            }
            writer = EgoLogWriter(self.log_path(name), metadata, self.config)
            self.writer = writer
            self.active = metadata
            writer.write_trigger(self.trigger.active, source_id)
            self.audio = AudioCapture(self.config["audio"], self._on_audio)
            try:
                self.audio.start()
            except Exception as exc:
                self.audio.error = str(exc)
                logging.exception("Audio input could not start")
            self.history.append(dict(metadata, status="running"))
            self._advance_counter(session_number)
            self._save_history()
            logging.info("Session started: %s", name)
            return dict(metadata)

    def stop_session(self) -> dict[str, Any]:
        with self.lock:
            if not self.writer or not self.active:
                raise RuntimeError("no active session")
            audio = self.audio
        if audio:
            audio.close()
        with self.lock:
            if not self.writer or not self.active:
                raise RuntimeError("session stopped concurrently")
            writer, metadata = self.writer, self.active
            metadata["stopped_utc"] = utc_now()
            metadata["duration_s"] = (
                time.monotonic_ns() - writer.started_monotonic_ns
            ) / 1_000_000_000
            writer.close(metadata)
            name = writer.path.name
            self.writer = None
            self.active = None
            self.audio = None
            for item in reversed(self.history):
                if item.get("log_name") == name:
                    item.update(metadata, status="completed")
                    break
            self._save_history()
            logging.info("Session stopped: %s", name)
            self.localpc.enqueue(name)
            return dict(metadata)

    def session_state(self) -> dict[str, Any]:
        with self.lock:
            active = dict(self.active) if self.active else None
            return {
                "active": active,
                "history": list(reversed(self.history[-30:])),
                "recording": bool(self.writer),
                "size": self.writer.size if self.writer else 0,
                "trigger_active": self.trigger.active,
                "audio": {
                    "enabled": bool(self.config["audio"].get("enabled")),
                    "blocks": self.audio.blocks if self.audio else 0,
                    "bytes": self.audio.bytes if self.audio else 0,
                    "error": self.audio.error if self.audio else "",
                },
            }

    def interfaces_state(self) -> dict[str, Any]:
        return {
            "config": {
                "source_id": int(self.config["source_id"]),
                "source_name": self.config.get("source_name", ""),
                "nmea": self.config["nmea"],
                "siren_trigger": self.config["siren_trigger"],
                "audio": self.config["audio"],
                "localpc": self.config.get("localpc", {}),
                "ego_sync": self.config.get("ego_sync", {}),
            },
            "nmea": self.nmea.status,
            "trigger": {
                "active": self.trigger.active,
                "error": self.trigger.error,
                "warning": self.trigger.warning,
                "mode": self.trigger.mode,
                "available": self.trigger.available,
            },
            "audio": {
                "active": bool(self.audio and self.audio.process),
                "blocks": self.audio.blocks if self.audio else 0,
                "bytes": self.audio.bytes if self.audio else 0,
                "error": self.audio.error if self.audio else "",
            },
            "ego_sync": self.ego_sync_state(),
        }

    def save_interfaces(self, raw: dict[str, Any]) -> dict[str, Any]:
        source_id = int(raw.get("source_id", self.config["source_id"]))
        if source_id < 1 or source_id > 3:
            raise ValueError("source_id must be 1..3")
        with self.lock:
            if self.writer:
                raise RuntimeError("cannot change interfaces during session")
        self._stop_inputs()
        with self.lock:
            previous_source_name = str(self.config.get("source_name", ""))
            previous_source_id = int(self.config["source_id"])
            self.config = deep_merge(
                self.config,
                {
                    "source_id": source_id,
                    "nmea": raw.get("nmea", {}),
                    "siren_trigger": raw.get("siren_trigger", {}),
                    "audio": raw.get("audio", {}),
                    "localpc": raw.get("localpc", {}),
                },
            )
            if previous_source_name in ("", f"Source {previous_source_id}"):
                self.config["source_name"] = f"Source {source_id}"
            self.save_config()
            self._start_inputs()
            return self.interfaces_state()

    def audio_devices(self, raw: dict[str, Any] | None = None) -> dict[str, Any]:
        config = dict(self.config.get("audio", {}))
        if raw:
            config.update(raw.get("audio", raw))
        return AudioCapture.list_windows_devices(config)

    @staticmethod
    def _history_by_log(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            str(item.get("log_name")): item
            for item in history
            if item.get("log_name")
        }

    def logs_state(self) -> dict[str, Any]:
        history = self._history_by_log(self.history)
        logs = []
        for path in sorted(
            self.logs_dir.glob("SRC*.bin"),
            key=lambda item: item.stat().st_mtime, reverse=True,
        ):
            stat = path.stat()
            localpc = self.localpc.record(path.name)
            session = history.get(path.name, {})
            logs.append({
                "name": path.name, "size": stat.st_size,
                "modified_utc": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
                "session_number": session.get("session_number", ""),
                "test_id": session.get("test_id", ""),
                "repeat_number": session.get("repeat_number", ""),
                "status": localpc.get("status", "local"),
                "progress_bytes": localpc.get("progress_bytes", 0),
                "error": localpc.get("error", ""),
                "localpc": localpc,
            })
        return {
            "logs": logs,
            "local_dir": str(self.logs_dir),
            "local_free_bytes": shutil.disk_usage(self.logs_dir).free,
            "localpc": self.localpc.settings(),
        }

    def uploads_state(self) -> dict[str, Any]:
        return self.logs_state()

    def send_log_to_localpc(self, name: str) -> dict[str, Any]:
        queued = self.localpc.enqueue(name, force=True)
        return {"ok": queued, "record": self.localpc.record(name)}

    def analyze_log(self, name: str) -> dict[str, Any]:
        path = self.log_path(name)
        stat = path.stat()
        gps = {
            "fixes": 0,
            "valid_fixes": 0,
            "rtk_float": 0,
            "rtk_fixed": 0,
            "max_satellites": 0,
            "max_fix_gap_s": 0.0,
            "last": None,
        }
        integrity = {
            "frames": 0,
            "gps_frames": 0,
            "header_crc_errors": 0,
            "payload_crc_errors": 0,
            "truncated": False,
            "error": "",
        }
        previous_gps_t_ns: int | None = None
        try:
            with path.open("rb") as stream:
                while True:
                    header_bytes = stream.read(FRAME_HEADER.size)
                    if not header_bytes:
                        break
                    if len(header_bytes) != FRAME_HEADER.size:
                        integrity["truncated"] = True
                        break
                    header = FRAME_HEADER.unpack(header_bytes)
                    if header[0] != FRAME_MAGIC:
                        integrity["error"] = "invalid frame magic"
                        break
                    payload_size = int(header[10])
                    if payload_size < 0 or payload_size > MAX_FRAME_PAYLOAD:
                        integrity["error"] = "invalid payload size"
                        break
                    checked = bytearray(header_bytes)
                    checked[64:68] = b"\0\0\0\0"
                    if zlib.crc32(checked) & 0xFFFFFFFF != int(header[12]):
                        integrity["header_crc_errors"] += 1
                    payload = stream.read(payload_size)
                    if len(payload) != payload_size:
                        integrity["truncated"] = True
                        break
                    if zlib.crc32(payload) & 0xFFFFFFFF != int(header[11]):
                        integrity["payload_crc_errors"] += 1
                    integrity["frames"] += 1
                    if int(header[3]) != TYPE_GPS_FIX or len(payload) < GPS_FIX.size:
                        continue
                    values = GPS_FIX.unpack_from(payload)
                    integrity["gps_frames"] += 1
                    gps["fixes"] += 1
                    t_ns = int(values[0])
                    if previous_gps_t_ns is not None:
                        gps["max_fix_gap_s"] = max(
                            gps["max_fix_gap_s"],
                            max(0.0, (t_ns - previous_gps_t_ns) / 1_000_000_000),
                        )
                    previous_gps_t_ns = t_ns
                    fix_type = int(values[8])
                    rtk_status = int(values[9])
                    satellites = int(values[10])
                    lat = float(values[1])
                    lon = float(values[2])
                    if fix_type > 0 and math.isfinite(lat) and math.isfinite(lon):
                        gps["valid_fixes"] += 1
                    gps["rtk_float"] += 1 if rtk_status == 1 else 0
                    gps["rtk_fixed"] += 1 if rtk_status == 2 else 0
                    gps["max_satellites"] = max(gps["max_satellites"], satellites)
                    gps["last"] = {
                        "latitude_deg": lat,
                        "longitude_deg": lon,
                        "altitude_m": float(values[3]),
                        "speed_kph": float(values[4]) * 3.6,
                        "heading_deg": math.degrees(float(values[5])),
                        "h_accuracy_m": float(values[6]),
                        "v_accuracy_m": float(values[7]),
                        "fix_type": fix_type,
                        "rtk_status": rtk_status,
                        "satellites": satellites,
                        "utc_time_ns": int(values[13]),
                        "hdop": float(values[14]),
                        "pdop": float(values[15]),
                        "vdop": float(values[16]),
                        "age_of_diff_s": float(values[17]),
                        "base_station_id": int(values[18]),
                        "gst_latitude_error_m": float(values[19]),
                        "gst_longitude_error_m": float(values[20]),
                        "gst_altitude_error_m": float(values[21]),
                        "gst_rms_error_m": float(values[22]),
                        "nmea_flags": int(values[23]),
                    }
        except OSError as exc:
            integrity["error"] = str(exc)
        status = "ok"
        reasons: list[str] = []
        if integrity["error"] or integrity["truncated"] or integrity["header_crc_errors"] or integrity["payload_crc_errors"]:
            status = "error"
            reasons.append("есть ошибки целостности файла")
        if gps["fixes"] == 0:
            status = "error"
            reasons.append("GPS-кадры отсутствуют")
        elif gps["valid_fixes"] == 0:
            status = "error"
            reasons.append("нет валидных GPS fix")
        elif gps["valid_fixes"] < gps["fixes"]:
            status = "warn" if status == "ok" else status
            reasons.append("часть GPS fix невалидна")
        if float(gps["max_fix_gap_s"]) > 2.0:
            status = "warn" if status == "ok" else status
            reasons.append(f"максимальный разрыв GPS {gps['max_fix_gap_s']:.2f} с")
        if not reasons:
            reasons.append("GPS-сигнал по логу выглядит исправным")
        return {
            "name": path.name,
            "size": stat.st_size,
            "modified_utc": datetime.fromtimestamp(
                stat.st_mtime, timezone.utc
            ).isoformat(),
            "status": status,
            "reasons": reasons,
            "integrity": integrity,
            "gps": gps,
        }

    def delete_log(self, name: str) -> None:
        with self.lock:
            if self.active and self.active.get("log_name") == name:
                raise RuntimeError("cannot delete active log")
            self.log_path(name).unlink()

    def set_logs_dir(self, value: str) -> dict[str, Any]:
        with self.lock:
            if self.writer:
                raise RuntimeError("cannot change directory during session")
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = (ROOT / path).resolve()
            path.mkdir(parents=True, exist_ok=True)
            self.config["storage"]["logs_dir"] = str(path)
            self.save_config()
            return self.uploads_state()

    def close(self) -> None:
        if self.writer:
            self.stop_session()
        self.ego_sync.close()
        self.localpc.close()
        self._stop_inputs()


class Handler(BaseHTTPRequestHandler):
    app: SourceApplication
    log_handler: RingLogHandler

    def log_message(self, format: str, *args: Any) -> None:
        logging.info("HTTP " + format, *args)

    def _json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def _static(self, name: str, content_type: str) -> None:
        data = (ROOT / "static" / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _download(self, name: str) -> None:
        path = self.app.log_path(name)
        size = path.stat().st_size
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        with path.open("rb") as stream:
            shutil.copyfileobj(stream, self.wfile, 1024 * 256)

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._static("index.html", "text/html; charset=utf-8")
            elif parsed.path == "/app.js":
                self._static("app.js", "application/javascript; charset=utf-8")
            elif parsed.path == "/style.css":
                self._static("style.css", "text/css; charset=utf-8")
            elif parsed.path == "/api/state":
                self._json({
                    "source_id": self.app.config["source_id"],
                    "source_name": self.app.config["source_name"],
                    "session": self.app.session_state(),
                    "interfaces": self.app.interfaces_state(),
                })
            elif parsed.path == "/api/sessions/catalog":
                self._json(self.app.session_catalog())
            elif parsed.path == "/api/sessions/state":
                self._json(self.app.session_state())
            elif parsed.path == "/api/uploads/state":
                self._json(self.app.uploads_state())
            elif parsed.path == "/api/uploads/local":
                name = parse_qs(parsed.query).get("name", [""])[0]
                self._download(name)
            elif parsed.path == "/api/logs/state":
                self._json(self.app.logs_state())
            elif parsed.path == "/api/logs/local":
                name = parse_qs(parsed.query).get("name", [""])[0]
                self._download(name)
            elif parsed.path == "/api/logs/analyze":
                name = parse_qs(parsed.query).get("name", [""])[0]
                self._json(self.app.analyze_log(name))
            elif parsed.path == "/api/interfaces":
                self._json(self.app.interfaces_state())
            elif parsed.path == "/api/ego-sync/state":
                self._json(self.app.ego_sync_state())
            elif parsed.path == "/api/audio/devices":
                self._json(self.app.audio_devices())
            elif parsed.path == "/api/journal":
                query = parse_qs(parsed.query)
                after_text = query.get("after", [""])[0]
                self._json(self.log_handler.state(
                    int(after_text) if after_text else None
                ))
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:
            logging.exception("GET failed")
            self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def do_POST(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/sessions/start":
                self._json(self.app.start_session(self._body()))
            elif parsed.path == "/api/sessions/stop":
                self._json(self.app.stop_session())
            elif parsed.path == "/api/uploads/config":
                self._json(self.app.uploader.configure(self._body()))
            elif parsed.path == "/api/uploads/test":
                self._json(self.app.uploader.test())
            elif parsed.path == "/api/uploads/upload":
                name = str(self._body()["name"])
                self.app.uploader.enqueue(name)
                self._json({"ok": True})
            elif parsed.path == "/api/logs/send":
                self._json(self.app.send_log_to_localpc(str(self._body()["name"])))
            elif parsed.path == "/api/uploads/delete":
                self.app.delete_log(str(self._body()["name"]))
                self._json({"ok": True})
            elif parsed.path == "/api/logs/delete":
                self.app.delete_log(str(self._body()["name"]))
                self._json({"ok": True})
            elif parsed.path == "/api/uploads/cancel":
                self._json({
                    "ok": self.app.uploader.cancel(str(self._body()["name"]))
                })
            elif parsed.path == "/api/uploads/local-dir":
                self._json(self.app.set_logs_dir(str(self._body()["path"])))
            elif parsed.path == "/api/interfaces":
                self._json(self.app.save_interfaces(self._body()))
            elif parsed.path == "/api/ego-sync/local":
                self._json(self.app.update_ego_sync_local_fields(self._body()))
            elif parsed.path == "/api/ego-sync/config":
                self._json(self.app.set_ego_sync_config(self._body()))
            elif parsed.path == "/api/audio/devices":
                self._json(self.app.audio_devices(self._body()))
            elif parsed.path == "/api/interfaces/simulate-trigger":
                self.app.trigger.simulate(bool(self._body()["active"]), force=True)
                self._json({"ok": True, "active": self.app.trigger.active})
            elif parsed.path == "/api/journal/clear":
                self.log_handler.clear()
                self._json({"ok": True})
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            logging.exception("POST failed")
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--bind")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    ring = RingLogHandler()
    ring.setFormatter(logging.Formatter("%(message)s"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger().addHandler(ring)

    app = SourceApplication(args.config.resolve())
    Handler.app = app
    Handler.log_handler = ring
    bind = args.bind or app.config["web"]["bind"]
    port = args.port or int(app.config["web"]["port"])
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    logging.info(
        "Source Sirena %s listening on http://%s:%s",
        app.config["source_id"], bind, port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
