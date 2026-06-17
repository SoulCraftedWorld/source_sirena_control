from __future__ import annotations

import json
from pathlib import Path
import struct
import threading
import time
import uuid
import zlib
from typing import Any


FRAME_MAGIC = 0x314F4745
PROTOCOL_VERSION = 2
FRAME_HEADER = struct.Struct("<IHHIIQQQQQIIII")
SESSION_EVENT_HEADER = struct.Struct("<IHHIIQQQIIII")
CONFIG_HEADER = struct.Struct("<IHHIIQIIIIIII")
AUDIO_HEADER = struct.Struct("<QQQIHHIIII")
GPS_FIX = struct.Struct("<QdddffffBBBBIQffffIffffI")
TIME_STATUS = struct.Struct("<QQqIIff")

TYPE_SESSION_STARTED = 1
TYPE_CONFIG_SNAPSHOT = 2
TYPE_AUDIO_BLOCK = 100
TYPE_GPS_FIX = 105
TYPE_TIME_STATUS = 200
TYPE_MARKER_EVENT = 203
TYPE_SESSION_ENDED = 900

FLAG_BINARY = 1 << 1
FLAG_KEYFRAME = 1 << 3
SESSION_MAGIC = 0x31534553
CONFIG_MAGIC = 0x31474643

GPS_NMEA_FLAG_UTC_TIME_VALID = 1 << 0
GPS_NMEA_FLAG_POSITION_VALID = 1 << 1
GPS_NMEA_FLAG_ALTITUDE_VALID = 1 << 2
GPS_NMEA_FLAG_HDOP_VALID = 1 << 3
GPS_NMEA_FLAG_PDOP_VALID = 1 << 4
GPS_NMEA_FLAG_VDOP_VALID = 1 << 5
GPS_NMEA_FLAG_AGE_DIFF_VALID = 1 << 6
GPS_NMEA_FLAG_BASE_STATION_VALID = 1 << 7
GPS_NMEA_FLAG_SPEED_VALID = 1 << 8
GPS_NMEA_FLAG_HEADING_VALID = 1 << 9
GPS_NMEA_FLAG_GST_LAT_VALID = 1 << 10
GPS_NMEA_FLAG_GST_LON_VALID = 1 << 11
GPS_NMEA_FLAG_GST_ALT_VALID = 1 << 12
GPS_NMEA_FLAG_GST_RMS_VALID = 1 << 13
GPS_NMEA_FLAG_ZDA_TIME_USED = 1 << 14


class EgoLogWriter:
    def __init__(self, path: Path, metadata: dict[str, Any], config: dict[str, Any]):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = path.open("wb")
        self._lock = threading.Lock()
        session_uuid = uuid.uuid4().int
        self.session_hi = session_uuid >> 64
        self.session_lo = session_uuid & ((1 << 64) - 1)
        self.sequence = 0
        self.audio_block_id = 0
        self.closed = False
        self.started_monotonic_ns = time.monotonic_ns()
        self._write_session_event(TYPE_SESSION_STARTED, metadata, "start")
        self._write_config(config)

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def _write_frame(
        self, frame_type: int, flags: int, t0_ns: int, t1_ns: int, payload: bytes
    ) -> None:
        payload_crc = zlib.crc32(payload) & 0xFFFFFFFF
        header = FRAME_HEADER.pack(
            FRAME_MAGIC, PROTOCOL_VERSION, FRAME_HEADER.size,
            frame_type, flags, self.session_hi, self.session_lo,
            self.sequence, t0_ns, t1_ns, len(payload), payload_crc, 0, 0,
        )
        header_crc = zlib.crc32(header) & 0xFFFFFFFF
        header = FRAME_HEADER.pack(
            FRAME_MAGIC, PROTOCOL_VERSION, FRAME_HEADER.size,
            frame_type, flags, self.session_hi, self.session_lo,
            self.sequence, t0_ns, t1_ns, len(payload), payload_crc,
            header_crc, 0,
        )
        with self._lock:
            if self.closed:
                return
            self._stream.write(header)
            self._stream.write(payload)
            self.sequence += 1

    @staticmethod
    def _metadata_text(metadata: dict[str, Any], event: str, reason: str) -> bytes:
        test_json = json.dumps(
            metadata, ensure_ascii=False, separators=(",", ":")
        )
        return (
            f"event={event}\n"
            f"source_id={metadata['source_id']}\n"
            f"source_name={metadata['source_name']}\n"
            f"correlation_key={metadata['correlation_key']}\n"
            f"test_metadata={test_json}\n"
            f"reason={reason}\n"
        ).encode("utf-8")

    def _write_session_event(
        self, frame_type: int, metadata: dict[str, Any], reason: str
    ) -> None:
        now_ns = time.monotonic_ns()
        text = self._metadata_text(
            metadata, "start" if frame_type == TYPE_SESSION_STARTED else "end",
            reason,
        )
        payload = SESSION_EVENT_HEADER.pack(
            SESSION_MAGIC, 1, SESSION_EVENT_HEADER.size, frame_type, 0x03,
            self.session_hi, self.session_lo, now_ns, len(text),
            zlib.crc32(text) & 0xFFFFFFFF, 1, 0,
        ) + text
        self._write_frame(
            frame_type, FLAG_BINARY | FLAG_KEYFRAME, now_ns, now_ns, payload
        )

    def _write_config(self, config: dict[str, Any]) -> None:
        now_ns = time.monotonic_ns()
        lines: list[str] = []
        for section, values in config.items():
            if isinstance(values, dict):
                lines.extend(f"{section}.{key}={value}" for key, value in values.items())
            else:
                lines.append(f"{section}={values}")
        text = ("\n".join(lines) + "\n").encode("utf-8")
        payload = CONFIG_HEADER.pack(
            CONFIG_MAGIC, 1, CONFIG_HEADER.size, 1, 0, now_ns,
            len(text), zlib.crc32(text) & 0xFFFFFFFF, 1, 0, 0, 0, 0,
        ) + text
        self._write_frame(
            TYPE_CONFIG_SNAPSHOT, FLAG_BINARY | FLAG_KEYFRAME,
            now_ns, now_ns, payload,
        )

    def write_gps(self, fix: dict[str, Any]) -> None:
        t_ns = int(fix.get("t_ns") or time.monotonic_ns())
        payload = GPS_FIX.pack(
            t_ns, float(fix.get("latitude_deg", 0.0)),
            float(fix.get("longitude_deg", 0.0)),
            float(fix.get("altitude_m", 0.0)),
            float(fix.get("speed_mps", 0.0)),
            float(fix.get("heading_rad", 0.0)),
            float(fix.get("h_accuracy_m", 0.0)),
            float(fix.get("v_accuracy_m", 0.0)),
            int(fix.get("fix_type", 0)), int(fix.get("rtk_status", 0)),
            int(fix.get("satellites", 0)), 0, int(fix.get("flags", 0)),
            int(fix.get("utc_ns", fix.get("utc_time_ns", 0)) or 0),
            float(fix.get("hdop", 0.0)),
            float(fix.get("pdop", 0.0)),
            float(fix.get("vdop", 0.0)),
            float(fix.get("age_of_diff_s", 0.0)),
            int(fix.get("base_station_id", 0) or 0),
            float(fix.get("gst_latitude_error_m", 0.0)),
            float(fix.get("gst_longitude_error_m", 0.0)),
            float(fix.get("gst_altitude_error_m", 0.0)),
            float(fix.get("gst_rms_error_m", 0.0)),
            int(fix.get("nmea_flags", 0)),
        )
        self._write_frame(TYPE_GPS_FIX, FLAG_BINARY, t_ns, t_ns, payload)
        utc_ns = fix.get("utc_ns")
        if utc_ns is not None:
            time_payload = TIME_STATUS.pack(
                t_ns, t_ns, int(utc_ns) - t_ns, 2, 1, 0.0, 0.0
            )
            self._write_frame(
                TYPE_TIME_STATUS, FLAG_BINARY, t_ns, t_ns, time_payload
            )

    def write_trigger(self, active: bool, source_id: int) -> None:
        t_ns = time.monotonic_ns()
        payload = json.dumps(
            {
                "event": "siren_trigger",
                "source_id": source_id,
                "active": active,
                "t_monotonic_ns": t_ns,
                "utc_ns": time.time_ns(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self._write_frame(TYPE_MARKER_EVENT, FLAG_BINARY, t_ns, t_ns, payload)

    def write_audio(
        self, data: bytes, sample_rate: int, channels: int,
        bytes_per_sample: int, frames: int,
    ) -> None:
        t1_ns = time.monotonic_ns()
        t0_ns = t1_ns - int(frames * 1_000_000_000 / sample_rate)
        payload = AUDIO_HEADER.pack(
            self.audio_block_id, t0_ns, t1_ns, sample_rate, channels,
            bytes_per_sample, frames, 1, len(data), 0,
        ) + data
        self.audio_block_id += 1
        self._write_frame(TYPE_AUDIO_BLOCK, FLAG_BINARY, t0_ns, t1_ns, payload)

    def close(self, metadata: dict[str, Any], reason: str = "stop") -> None:
        if self.closed:
            return
        self._write_session_event(TYPE_SESSION_ENDED, metadata, reason)
        with self._lock:
            self._stream.flush()
            self._stream.close()
            self.closed = True
