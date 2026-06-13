from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from ego_log import EgoLogWriter
from inputs import AudioCapture, NmeaService, TriggerService


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = ROOT / "session_history.json"
LOG_NAME_RE = re.compile(r"^SRC[1-3]_S[A-Za-z0-9_.-]+\.bin$")

TEST_CATALOG = [
    {"group": "LAB", "id": "LAB-01", "name": "Сирена, тип 1"},
    {"group": "LAB", "id": "LAB-02", "name": "Сирена, тип 2"},
    {"group": "FT-D6", "id": "FT-D6.1", "name": "Равномерное движение 50 км/ч"},
    {"group": "FT-D6", "id": "FT-D6.2", "name": "Плавный разгон 0-80 км/ч"},
    {"group": "FT-D6", "id": "FT-D6.3", "name": "Торможение 80-0 км/ч"},
    {"group": "CUSTOM", "id": "CUSTOM", "name": "Пользовательское испытание"},
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self.history = self._load_history()
        self.uploader = S3Uploader(self)
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

    def save_config(self) -> None:
        atomic_json(self.config_path, self.config)

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

    def start_session(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.writer:
                raise RuntimeError("session already active")
            source_id = int(self.config["source_id"])
            session_number = self._clean(raw.get("session_number"), 24)
            test_id = self._clean(raw.get("test_id"), 32)
            repeat = max(1, int(raw.get("repeat_number", 1)))
            if not session_number or not test_id:
                raise ValueError("session number and test ID are required")
            correlation = f"S{session_number}_{test_id}_R{repeat}"
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            name = f"SRC{source_id}_{correlation}_{timestamp}.bin"
            metadata = dict(raw)
            metadata.update(
                source_id=source_id,
                source_name=self.config["source_name"],
                source_role="siren_source",
                correlation_key=correlation,
                repeat_number=repeat,
                started_utc=utc_now(),
                log_name=name,
            )
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
            if self.config["s3"].get("auto_upload"):
                self.uploader.enqueue(name)
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
                "nmea": self.config["nmea"],
                "siren_trigger": self.config["siren_trigger"],
                "audio": self.config["audio"],
            },
            "nmea": self.nmea.status,
            "trigger": {
                "active": self.trigger.active,
                "error": self.trigger.error,
            },
            "audio": {
                "active": bool(self.audio and self.audio.process),
                "blocks": self.audio.blocks if self.audio else 0,
                "bytes": self.audio.bytes if self.audio else 0,
                "error": self.audio.error if self.audio else "",
            },
        }

    def save_interfaces(self, raw: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if self.writer:
                raise RuntimeError("cannot change interfaces during session")
        self._stop_inputs()
        with self.lock:
            self.config = deep_merge(
                self.config,
                {
                    "nmea": raw.get("nmea", {}),
                    "siren_trigger": raw.get("siren_trigger", {}),
                    "audio": raw.get("audio", {}),
                },
            )
            self.save_config()
            self._start_inputs()
            return self.interfaces_state()

    def uploads_state(self) -> dict[str, Any]:
        logs = []
        for path in sorted(
            self.logs_dir.glob("SRC*.bin"),
            key=lambda item: item.stat().st_mtime, reverse=True,
        ):
            stat = path.stat()
            logs.append({
                "name": path.name, "size": stat.st_size,
                "modified_utc": datetime.fromtimestamp(
                    stat.st_mtime, timezone.utc
                ).isoformat(),
            } | self.uploader.record(path.name))
        return {
            "logs": logs,
            "local_dir": str(self.logs_dir),
            "local_free_bytes": shutil.disk_usage(self.logs_dir).free,
            "settings": self.uploader.public_settings(),
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
                self._json({"tests": TEST_CATALOG})
            elif parsed.path == "/api/sessions/state":
                self._json(self.app.session_state())
            elif parsed.path == "/api/uploads/state":
                self._json(self.app.uploads_state())
            elif parsed.path == "/api/uploads/local":
                name = parse_qs(parsed.query).get("name", [""])[0]
                self._download(name)
            elif parsed.path == "/api/interfaces":
                self._json(self.app.interfaces_state())
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
            elif parsed.path == "/api/uploads/delete":
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
            elif parsed.path == "/api/interfaces/simulate-trigger":
                self.app.trigger.simulate(bool(self._body()["active"]))
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
