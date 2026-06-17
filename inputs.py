from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import math
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable


class InputUnavailableError(RuntimeError):
    """Configured physical input is not available on this host."""


def _nmea_checksum_valid(sentence: str) -> bool:
    text = sentence.strip()
    if not text.startswith("$") or "*" not in text:
        return False
    body, expected = text[1:].split("*", 1)
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    try:
        return checksum == int(expected[:2], 16)
    except ValueError:
        return False


def _coordinate(value: str, hemisphere: str) -> float:
    if not value:
        return 0.0
    raw = float(value)
    degrees = int(raw // 100)
    result = degrees + (raw - degrees * 100) / 60.0
    return -result if hemisphere in {"S", "W"} else result


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


def _float(value: str, default: float = 0.0) -> float:
    return float(value) if value else default


def _int(value: str, default: int = 0) -> int:
    return int(value) if value else default


class NmeaParser:
    def __init__(self) -> None:
        self._last: dict[str, Any] = {}
        self._utc_date: tuple[int, int, int] | None = None

    def _set_flag(self, flag: int) -> None:
        self._last["nmea_flags"] = int(self._last.get("nmea_flags", 0)) | flag

    def _accept_utc(self, time_text: str, date_text: str = "") -> None:
        if date_text and len(date_text) >= 6:
            year_value = int(date_text[4:6])
            day, month, year = (
                int(date_text[0:2]), int(date_text[2:4]),
                1900 + year_value if year_value >= 80 else 2000 + year_value,
            )
            self._utc_date = (year, month, day)
        if self._utc_date is None or len(time_text) < 6:
            return
        hour, minute = int(time_text[0:2]), int(time_text[2:4])
        second_value = float(time_text[4:])
        value = datetime(
            *self._utc_date, hour, minute, tzinfo=timezone.utc
        ) + timedelta(seconds=second_value)
        self._last["utc_ns"] = int(value.timestamp() * 1_000_000_000)
        self._set_flag(GPS_NMEA_FLAG_UTC_TIME_VALID)

    def _accept_zda_utc(
        self, time_text: str, day_text: str, month_text: str, year_text: str
    ) -> None:
        if not time_text or not day_text or not month_text or not year_text:
            return
        self._utc_date = (int(year_text), int(month_text), int(day_text))
        self._accept_utc(time_text)
        self._set_flag(GPS_NMEA_FLAG_ZDA_TIME_USED)

    def _publish(self) -> dict[str, Any]:
        self._last["t_ns"] = time.monotonic_ns()
        self._last["received_utc"] = datetime.now(timezone.utc).isoformat()
        return dict(self._last)

    def ingest(self, sentence: str) -> dict[str, Any] | None:
        if not _nmea_checksum_valid(sentence):
            return None
        fields = sentence.strip()[1:].split("*", 1)[0].split(",")
        kind = fields[0][-3:]
        try:
            if kind == "RMC" and len(fields) >= 10:
                valid = fields[2] == "A"
                self._accept_utc(fields[1], fields[9])
                if valid and fields[3] and fields[5]:
                    self._last.update(
                        latitude_deg=_coordinate(fields[3], fields[4]),
                        longitude_deg=_coordinate(fields[5], fields[6]),
                    )
                    self._set_flag(GPS_NMEA_FLAG_POSITION_VALID)
                if fields[7]:
                    self._last["speed_mps"] = _float(fields[7]) * 0.514444
                    self._set_flag(GPS_NMEA_FLAG_SPEED_VALID)
                if fields[8]:
                    self._last["heading_rad"] = math.radians(_float(fields[8]))
                    self._set_flag(GPS_NMEA_FLAG_HEADING_VALID)
                self._last.update(
                    fix_type=1 if valid else 0,
                    utc_text=f"{fields[9]} {fields[1]}",
                )
            elif kind == "GGA" and len(fields) >= 10:
                self._accept_utc(fields[1])
                quality = _int(fields[6])
                rtk = 2 if quality == 4 else 1 if quality == 5 else 0
                if fields[2] and fields[4]:
                    self._last.update(
                        latitude_deg=_coordinate(fields[2], fields[3]),
                        longitude_deg=_coordinate(fields[4], fields[5]),
                    )
                    self._set_flag(GPS_NMEA_FLAG_POSITION_VALID)
                if fields[9]:
                    self._last["altitude_m"] = _float(fields[9])
                    self._set_flag(GPS_NMEA_FLAG_ALTITUDE_VALID)
                if fields[8]:
                    hdop = _float(fields[8])
                    self._last["hdop"] = hdop
                    self._last["h_accuracy_m"] = hdop
                    self._set_flag(GPS_NMEA_FLAG_HDOP_VALID)
                if len(fields) > 13 and fields[13]:
                    self._last["age_of_diff_s"] = _float(fields[13])
                    self._set_flag(GPS_NMEA_FLAG_AGE_DIFF_VALID)
                if len(fields) > 14 and fields[14]:
                    self._last["base_station_id"] = _int(fields[14])
                    self._set_flag(GPS_NMEA_FLAG_BASE_STATION_VALID)
                self._last.update(
                    satellites=_int(fields[7]),
                    fix_type=quality,
                    rtk_status=rtk,
                )
            elif kind == "GSA" and len(fields) >= 18:
                fix_mode = _int(fields[2])
                if fix_mode:
                    self._last["fix_type"] = fix_mode
                if fields[15]:
                    self._last["pdop"] = _float(fields[15])
                    self._set_flag(GPS_NMEA_FLAG_PDOP_VALID)
                if fields[16]:
                    hdop = _float(fields[16])
                    self._last["hdop"] = hdop
                    self._last["h_accuracy_m"] = hdop
                    self._set_flag(GPS_NMEA_FLAG_HDOP_VALID)
                if fields[17]:
                    self._last["vdop"] = _float(fields[17])
                    self._last["v_accuracy_m"] = _float(fields[17])
                    self._set_flag(GPS_NMEA_FLAG_VDOP_VALID)
            elif kind == "GST" and len(fields) >= 9:
                self._accept_utc(fields[1])
                if fields[2]:
                    self._last["gst_rms_error_m"] = _float(fields[2])
                    self._set_flag(GPS_NMEA_FLAG_GST_RMS_VALID)
                if fields[6]:
                    value = _float(fields[6])
                    self._last["gst_latitude_error_m"] = value
                    self._last["h_accuracy_m"] = value
                    self._set_flag(GPS_NMEA_FLAG_GST_LAT_VALID)
                if fields[7]:
                    self._last["gst_longitude_error_m"] = _float(fields[7])
                    self._set_flag(GPS_NMEA_FLAG_GST_LON_VALID)
                if fields[8]:
                    value = _float(fields[8])
                    self._last["gst_altitude_error_m"] = value
                    self._last["v_accuracy_m"] = value
                    self._set_flag(GPS_NMEA_FLAG_GST_ALT_VALID)
            elif kind == "VTG" and len(fields) >= 8:
                if fields[1]:
                    self._last["heading_rad"] = math.radians(_float(fields[1]))
                    self._set_flag(GPS_NMEA_FLAG_HEADING_VALID)
                if fields[7]:
                    self._last["speed_mps"] = _float(fields[7]) / 3.6
                    self._set_flag(GPS_NMEA_FLAG_SPEED_VALID)
                elif len(fields) > 5 and fields[5]:
                    self._last["speed_mps"] = _float(fields[5]) * 0.514444
                    self._set_flag(GPS_NMEA_FLAG_SPEED_VALID)
            elif kind == "ZDA" and len(fields) >= 5:
                self._accept_zda_utc(fields[1], fields[2], fields[3], fields[4])
            else:
                return None
        except (ValueError, OverflowError):
            return None
        return self._publish()


class NmeaService:
    def __init__(
        self, config: dict[str, Any], callback: Callable[[dict[str, Any]], None]
    ) -> None:
        self.config = config
        self.callback = callback
        self.parser = NmeaParser()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.status = {
            "running": False, "sentences": 0, "valid": 0, "errors": 0,
            "bytes": 0, "last_fix": None, "error": "", "available": False,
            "connected": False, "peer": "", "listen": "",
        }

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run, name="source-nmea", daemon=True
        )
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _accept(self, raw: bytes) -> None:
        self.status["bytes"] += len(raw)
        for line in raw.decode("ascii", errors="ignore").splitlines():
            if not line.strip():
                continue
            self.status["sentences"] += 1
            fix = self.parser.ingest(line)
            if fix is None:
                self.status["errors"] += 1
                continue
            self.status["valid"] += 1
            self.status["last_fix"] = fix
            self.callback(fix)

    def _run(self) -> None:
        kind = self.config.get("type", "disabled")
        try:
            self.status["source_type"] = kind
            if kind == "disabled":
                self.status["available"] = True
                while not self.stop_event.wait(1):
                    pass
                return
            if kind == "udp":
                self._run_udp()
            elif kind == "tcp":
                self._run_tcp()
            elif kind in {"usb", "uart"}:
                self._run_serial(kind)
            else:
                raise ValueError(f"unsupported NMEA input type: {kind}")
        except InputUnavailableError as exc:
            self.status["error"] = str(exc)
            logging.warning("NMEA input unavailable: %s", exc)
        except Exception as exc:
            self.status["error"] = str(exc)
            logging.exception("NMEA service stopped")
        finally:
            self.status["running"] = False

    def _run_udp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        sock.bind((
            str(self.config.get("udp_bind", "0.0.0.0")),
            int(self.config.get("udp_port", 10110)),
        ))
        self.status["available"] = True
        self.status["running"] = True
        with sock:
            while not self.stop_event.is_set():
                try:
                    data, _ = sock.recvfrom(8192)
                    self._accept(data)
                except socket.timeout:
                    continue

    def _run_tcp(self) -> None:
        bind = str(
            self.config.get("tcp_bind", self.config.get("udp_bind", "0.0.0.0"))
        )
        port = int(self.config.get("tcp_port", self.config.get("udp_port", 10110)))
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)
        sock.bind((bind, port))
        sock.listen(1)
        actual_host, actual_port = sock.getsockname()[:2]
        self.status["listen"] = f"{actual_host}:{actual_port}"
        self.status["available"] = True
        self.status["running"] = True
        with sock:
            while not self.stop_event.is_set():
                try:
                    conn, addr = sock.accept()
                except socket.timeout:
                    continue
                self._handle_tcp_client(conn, addr)

    def _handle_tcp_client(
        self, conn: socket.socket, addr: tuple[str, int]
    ) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        self.status["connected"] = True
        self.status["peer"] = peer
        buffer = bytearray()
        with conn:
            conn.settimeout(1.0)
            while not self.stop_event.is_set():
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError as exc:
                    self.status["error"] = f"NMEA TCP receive failed: {exc}"
                    break
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    line, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    if line:
                        self._accept(bytes(line.rstrip(b"\r")) + b"\n")
                if len(buffer) > 8192:
                    self.status["errors"] += 1
                    buffer.clear()
        self.status["connected"] = False
        self.status["peer"] = ""

    def _run_serial(self, kind: str) -> None:
        try:
            import serial
        except ImportError as exc:
            raise InputUnavailableError(
                "pyserial is required for USB/UART NMEA; "
                "install requirements.txt or select TCP/disabled"
            ) from exc
        device = self.config[f"{kind}_device"]
        baud = int(self.config.get(f"{kind}_baud", 115200))
        try:
            with serial.Serial(device, baudrate=baud, timeout=1) as stream:
                self.status["available"] = True
                self.status["running"] = True
                while not self.stop_event.is_set():
                    line = stream.readline()
                    if line:
                        self._accept(line)
        except (OSError, serial.SerialException) as exc:
            raise InputUnavailableError(
                f"cannot open NMEA {kind} device {device}: {exc}"
            ) from exc


class TriggerService:
    def __init__(
        self, config: dict[str, Any], callback: Callable[[bool], None]
    ) -> None:
        self.config = config
        self.callback = callback
        self.device: Any = None
        self.active = False
        self.error = ""
        self.warning = ""
        self.mode = "unavailable"
        self.available = False

    def start(self) -> None:
        requested_mode = str(self.config.get("mode", "")).strip().lower()
        if not requested_mode:
            requested_mode = "mock" if self.config.get("mock") else "auto"
        if requested_mode not in {"auto", "gpio", "mock"}:
            self.error = f"unsupported trigger mode: {requested_mode}"
            logging.error(self.error)
            return
        if requested_mode == "mock":
            self._enable_mock()
            return
        if requested_mode == "auto" and not sys.platform.startswith("linux"):
            self._enable_mock(
                f"GPIO is unavailable on {sys.platform}; using trigger simulation"
            )
            return
        try:
            from gpiozero import DigitalInputDevice
            pull = self.config.get("pull", "down")
            if pull not in {"down", "none"}:
                raise ValueError("positive-on input supports pull=down or none")
            self.device = DigitalInputDevice(
                int(self.config["gpio_bcm"]),
                pull_up=False if pull == "down" else None,
                active_state=True,
                bounce_time=float(self.config.get("debounce_ms", 10)) / 1000,
            )
            self.active = bool(self.device.is_active)
            self.device.when_activated = lambda: self._set(True)
            self.device.when_deactivated = lambda: self._set(False)
            self.mode = "gpio"
            self.available = True
        except Exception as exc:
            if requested_mode == "auto":
                self._enable_mock(f"GPIO unavailable; using simulation: {exc}")
            else:
                self.error = str(exc)
                logging.exception("GPIO trigger unavailable")

    def _enable_mock(self, warning: str = "") -> None:
        self.mode = "mock"
        self.available = True
        self.warning = warning
        if warning:
            logging.warning(warning)

    def _set(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        logging.info("Siren trigger %s", "ON" if active else "OFF")
        self.callback(active)

    def simulate(self, active: bool) -> None:
        if self.mode != "mock":
            raise RuntimeError("trigger simulation requires mock/auto fallback mode")
        self._set(active)

    def close(self) -> None:
        if self.device is not None:
            self.device.close()


class AudioCapture:
    def __init__(
        self, config: dict[str, Any],
        callback: Callable[[bytes, int, int, int, int], None],
    ) -> None:
        self.config = config
        self.callback = callback
        self.process: subprocess.Popen[bytes] | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.blocks = 0
        self.bytes = 0
        self.error = ""

    def start(self) -> None:
        if not self.config.get("enabled"):
            return
        rate = int(self.config["sample_rate_hz"])
        channels = int(self.config["channels"])
        sample_format = str(self.config.get("sample_format", "S32_LE"))
        command = [
            "arecord", "-q", "-D", str(self.config["alsa_device"]),
            "-f", sample_format, "-r", str(rate), "-c", str(channels),
            "-t", "raw",
        ]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE)
        self.thread = threading.Thread(
            target=self._run, name="source-audio", daemon=True
        )
        self.thread.start()

    def _run(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        rate = int(self.config["sample_rate_hz"])
        channels = int(self.config["channels"])
        sample_bytes = int(self.config["bytes_per_sample"])
        frames = int(self.config["block_frames"])
        block_size = frames * channels * sample_bytes
        try:
            while not self.stop_event.is_set():
                data = self.process.stdout.read(block_size)
                if len(data) != block_size:
                    break
                self.blocks += 1
                self.bytes += len(data)
                self.callback(data, rate, channels, sample_bytes, frames)
        except Exception as exc:
            self.error = str(exc)
            logging.exception("Audio capture stopped")

    def close(self) -> None:
        self.stop_event.set()
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.thread:
            self.thread.join(timeout=2)
