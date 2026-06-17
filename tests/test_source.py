from __future__ import annotations

import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zlib


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

import ego_log
from inputs import NmeaParser, NmeaService, TriggerService
import server


def nmea(body: str) -> str:
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"${body}*{checksum:02X}"


class EgoLogTests(unittest.TestCase):
    def test_writer_creates_crc_valid_frames_with_source_metadata(self) -> None:
        metadata = {
            "source_id": 2,
            "source_name": "Source 2",
            "correlation_key": "S5_FT-D6.1_R3",
            "test_id": "FT-D6.1",
        }
        config = json.loads(
            (ROOT / "config.example.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "SRC2_S5_FT-D6.1_R3_TEST.bin"
            writer = ego_log.EgoLogWriter(path, metadata, config)
            writer.write_trigger(True, 2)
            writer.write_gps({
                "latitude_deg": 53.9, "longitude_deg": 27.56,
                "fix_type": 1, "satellites": 10,
            })
            writer.close(metadata)
            data = path.read_bytes()

        offset = 0
        types = []
        while offset < len(data):
            header_bytes = data[offset:offset + ego_log.FRAME_HEADER.size]
            header = ego_log.FRAME_HEADER.unpack(header_bytes)
            checked = bytearray(header_bytes)
            checked[64:68] = b"\0\0\0\0"
            self.assertEqual(zlib.crc32(checked) & 0xFFFFFFFF, header[12])
            payload_start = offset + ego_log.FRAME_HEADER.size
            payload = data[payload_start:payload_start + header[10]]
            self.assertEqual(zlib.crc32(payload) & 0xFFFFFFFF, header[11])
            types.append(header[3])
            offset = payload_start + header[10]

        self.assertEqual(types[0], ego_log.TYPE_SESSION_STARTED)
        self.assertIn(ego_log.TYPE_CONFIG_SNAPSHOT, types)
        self.assertIn(ego_log.TYPE_MARKER_EVENT, types)
        self.assertIn(ego_log.TYPE_GPS_FIX, types)
        self.assertEqual(types[-1], ego_log.TYPE_SESSION_ENDED)

    def test_struct_sizes_match_ego_contract(self) -> None:
        self.assertEqual(ego_log.FRAME_HEADER.size, 72)
        self.assertEqual(ego_log.SESSION_EVENT_HEADER.size, 56)
        self.assertEqual(ego_log.CONFIG_HEADER.size, 52)
        self.assertEqual(ego_log.AUDIO_HEADER.size, 48)
        self.assertEqual(ego_log.GPS_FIX.size, 104)
        self.assertEqual(ego_log.TIME_STATUS.size, 40)


class NmeaTests(unittest.TestCase):
    def test_gga_and_rmc_are_parsed(self) -> None:
        parser = NmeaParser()
        gga = parser.ingest(
            "$GPGGA,123519,4807.038,N,01131.000,E,4,08,0.9,"
            "545.4,M,46.9,M,,*42"
        )
        self.assertIsNotNone(gga)
        assert gga is not None
        self.assertAlmostEqual(gga["latitude_deg"], 48.1173, places=4)
        self.assertEqual(gga["rtk_status"], 2)

        rmc = parser.ingest(
            "$GPRMC,123519,A,4807.038,N,01131.000,E,022.4,"
            "084.4,230394,003.1,W*6A"
        )
        self.assertIsNotNone(rmc)
        assert rmc is not None
        self.assertAlmostEqual(rmc["speed_mps"], 22.4 * 0.514444, places=5)
        self.assertIn("utc_ns", rmc)

    def test_gsa_gst_vtg_and_zda_extend_current_fix(self) -> None:
        parser = NmeaParser()
        self.assertIsNotNone(parser.ingest(nmea(
            "GPGGA,123519,4807.038,N,01131.000,E,4,08,0.9,"
            "545.4,M,46.9,M,1.2,1001"
        )))
        self.assertIsNotNone(parser.ingest(nmea(
            "GPGSA,A,3,04,05,09,12,24,25,29,31,,,,,1.8,0.9,1.5"
        )))
        self.assertIsNotNone(parser.ingest(nmea(
            "GPGST,123519,0.12,0.23,0.34,45.0,0.05,0.06,0.07"
        )))
        self.assertIsNotNone(parser.ingest(nmea(
            "GPVTG,084.4,T,,M,022.4,N,041.5,K"
        )))
        fix = parser.ingest(nmea("GPZDA,123519.00,23,03,1994,00,00"))
        self.assertIsNotNone(fix)
        assert fix is not None
        self.assertAlmostEqual(fix["pdop"], 1.8)
        self.assertAlmostEqual(fix["vdop"], 1.5)
        self.assertAlmostEqual(fix["gst_latitude_error_m"], 0.05)
        self.assertAlmostEqual(fix["gst_longitude_error_m"], 0.06)
        self.assertAlmostEqual(fix["gst_altitude_error_m"], 0.07)
        self.assertAlmostEqual(fix["gst_rms_error_m"], 0.12)
        self.assertAlmostEqual(fix["age_of_diff_s"], 1.2)
        self.assertEqual(fix["base_station_id"], 1001)
        self.assertAlmostEqual(fix["speed_mps"], 41.5 / 3.6, places=5)
        self.assertIn("utc_ns", fix)

    def test_tcp_service_accepts_nmea_without_optional_serial_dependency(self) -> None:
        service = NmeaService(
            {"type": "tcp", "tcp_bind": "127.0.0.1", "tcp_port": 0},
            lambda fix: None,
        )
        service.start()
        try:
            deadline = time.monotonic() + 1
            while not service.status["running"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(service.status["running"])
            self.assertTrue(service.status["available"])
            self.assertEqual(service.status["error"], "")
        finally:
            service.close()

    def test_tcp_service_parses_received_nmea(self) -> None:
        fixes: list[dict[str, object]] = []
        service = NmeaService(
            {"type": "tcp", "tcp_bind": "127.0.0.1", "tcp_port": 0},
            fixes.append,
        )
        service.start()
        try:
            deadline = time.monotonic() + 1
            while not service.status["running"] and time.monotonic() < deadline:
                time.sleep(0.01)
            port = int(str(service.status["listen"]).rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=1) as client:
                client.sendall(
                    b"$GPGGA,123519,4807.038,N,01131.000,E,4,08,0.9,"
                    b"545.4,M,46.9,M,,*42\r\n"
                )
            deadline = time.monotonic() + 1
            while not fixes and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(service.status["valid"], 1)
            self.assertEqual(service.status["errors"], 0)
            self.assertAlmostEqual(
                float(fixes[0]["latitude_deg"]), 48.1173, places=4
            )
        finally:
            service.close()


class TriggerTests(unittest.TestCase):
    def test_auto_mode_uses_mock_outside_linux(self) -> None:
        changes: list[bool] = []
        service = TriggerService(
            {"mode": "auto", "gpio_bcm": 17}, changes.append
        )
        with mock.patch("inputs.sys.platform", "win32"):
            service.start()
        self.assertEqual(service.mode, "mock")
        self.assertTrue(service.available)
        self.assertIn("using trigger simulation", service.warning)
        service.simulate(True)
        self.assertTrue(service.active)
        self.assertEqual(changes, [True])

    def test_explicit_gpio_mode_does_not_fallback_to_mock(self) -> None:
        service = TriggerService(
            {"mode": "gpio", "gpio_bcm": 17}, lambda active: None
        )
        with (
            mock.patch("inputs.sys.platform", "win32"),
            mock.patch.dict(sys.modules, {"gpiozero": None}),
        ):
            service.start()
        self.assertNotEqual(service.mode, "mock")
        self.assertFalse(service.available)
        self.assertTrue(service.error)


class ConfigTests(unittest.TestCase):
    def test_default_config_is_portable(self) -> None:
        example = json.loads(
            (ROOT / "config.example.json").read_text(encoding="utf-8")
        )
        self.assertEqual(example["nmea"]["type"], "tcp")
        self.assertEqual(example["siren_trigger"]["mode"], "auto")

    def test_test_catalog_matches_ego_scenarios(self) -> None:
        ids = {item["id"] for item in server.TEST_CATALOG}
        self.assertEqual(len(server.TEST_CATALOG), 40)
        self.assertIn("LAB-04", ids)
        self.assertIn("FT-S-FRONT", ids)
        self.assertIn("FT-D9-B", ids)
        self.assertIn("FT-Multi.04", ids)
        self.assertIn("FT-N.08", ids)
        self.assertIn("CUSTOM", ids)

    def test_source_id_is_limited_to_three_sources(self) -> None:
        example = json.loads(
            (ROOT / "config.example.json").read_text(encoding="utf-8")
        )
        example["source_id"] = 4
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(example), encoding="utf-8")
            with self.assertRaises(ValueError):
                server.load_config(path)

    def test_mock_session_records_trigger_and_completes(self) -> None:
        example = json.loads(
            (ROOT / "config.example.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example["source_id"] = 2
            example["storage"]["logs_dir"] = str(root / "logs")
            example["nmea"]["type"] = "disabled"
            example["siren_trigger"]["mock"] = True
            example["audio"]["enabled"] = False
            config_path = root / "config.json"
            config_path.write_text(json.dumps(example), encoding="utf-8")
            previous_history = server.HISTORY_PATH
            server.HISTORY_PATH = root / "history.json"
            app = server.SourceApplication(config_path)
            try:
                session = app.start_session({
                    "session_number": "5",
                    "test_group": "WRONG",
                    "test_id": "FT-D6.1",
                    "test_name": "",
                    "repeat_number": 3,
                    "siren_type": "Полиция",
                    "ego_speed_kph": 50,
                    "precipitation_rate_mmh": 1.5,
                })
                app.trigger.simulate(True)
                result = app.stop_session()
                path = app.log_path(session["log_name"])
                self.assertTrue(path.is_file())
                self.assertEqual(result["correlation_key"], "S5_FT-D6.1_R3")
                self.assertEqual(result["test_group"], "FT-D6")
                self.assertEqual(
                    result["test_name"], "Равномерное движение 50 км/ч"
                )
                self.assertEqual(result["siren_type"], "Полиция")
                self.assertNotIn("ego_speed_kph", result)
                self.assertNotIn("precipitation_rate_mmh", result)
                self.assertEqual(app.session_catalog()["session_numbers"], ["5"])
                self.assertFalse(app.session_state()["recording"])
            finally:
                app.close()
                server.HISTORY_PATH = previous_history

    def test_completed_session_is_pushed_to_localpc_tcp(self) -> None:
        example = json.loads(
            (ROOT / "config.example.json").read_text(encoding="utf-8")
        )
        received: dict[str, object] = {}
        ready = threading.Event()
        done = threading.Event()

        def tcp_receiver(listener: socket.socket) -> None:
            listener.listen(1)
            ready.set()
            conn, _ = listener.accept()
            with conn, conn.makefile("rb") as stream:
                header = json.loads(stream.readline().decode("utf-8"))
                payload = stream.read(int(header["size"]))
                received["header"] = header
                received["payload_size"] = len(payload)
                conn.sendall(b"OK stored\n")
            done.set()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            thread = threading.Thread(
                target=tcp_receiver, args=(listener,), daemon=True
            )
            thread.start()
            self.assertTrue(ready.wait(1.0))

            example["source_id"] = 2
            example["storage"]["logs_dir"] = str(root / "logs")
            example["nmea"]["type"] = "disabled"
            example["siren_trigger"]["mock"] = True
            example["audio"]["enabled"] = False
            example["localpc"].update(
                {
                    "enabled": True,
                    "host": "127.0.0.1",
                    "port": port,
                    "retry_window_s": 2.0,
                    "retry_interval_s": 0.1,
                }
            )
            config_path = root / "config.json"
            config_path.write_text(json.dumps(example), encoding="utf-8")
            previous_history = server.HISTORY_PATH
            server.HISTORY_PATH = root / "history.json"
            app = server.SourceApplication(config_path)
            try:
                session = app.start_session({
                    "session_number": "5",
                    "test_id": "FT-D6.1",
                    "repeat_number": 3,
                })
                app.stop_session()
                self.assertTrue(done.wait(2.0))
                header = received["header"]
                self.assertEqual(header["name"], session["log_name"])
                self.assertEqual(header["correlation_key"], "S5_FT-D6.1_R3")
                self.assertGreater(received["payload_size"], 0)
            finally:
                app.close()
                server.HISTORY_PATH = previous_history
                listener.close()


if __name__ == "__main__":
    unittest.main()
