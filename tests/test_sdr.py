"""SDR backends, rtl_tcp framing, platform guidance and WebSDR helpers."""
from __future__ import annotations

import struct

import numpy as np


def _reset_detect(monkeypatch, data):
    from darksec import sdr

    monkeypatch.setattr(sdr, "detect", lambda force=False: data)


def test_rtl_test_output_is_parsed():
    from darksec import sdr

    out = """Found 1 device(s):
  0:  Realtek, RTL2838UHIDIR, SN: 00000001

Using device 0: Generic RTL2832U OEM
Detached kernel driver
Found Rafael Micro R820T tuner
Supported gain values (29): 0.0 0.9 1.4 2.7 3.7 7.7 8.7 12.5 14.4 15.7
[R82XX] PLL not locked!
Sampling at 2048000 S/s.
"""
    parsed = sdr._parse_rtl_test(out)
    assert parsed["device_count"] == 1
    assert parsed["tuner"] == "Rafael Micro R820T"
    assert parsed["sample_rate"] == 2048000
    assert len(parsed["gains"]) == 10
    assert parsed["error"] == ""


def test_rtl_power_csv_line_is_parsed():
    from darksec import sdr

    line = "2026-11-19, 12:00:00, 88000000, 88125000, 125000, 3, -45.2, -50.1, -60.0"
    frame = sdr._parse_rtl_power_line(line)
    assert frame["low_hz"] == 88_000_000
    assert frame["high_hz"] == 88_125_000
    assert frame["bin_hz"] == 125_000
    assert frame["db"] == [-45.2, -50.1, -60.0]
    assert frame["stamp"].startswith("2026-11-19")


def test_unsigned_rtl_iq_is_centred(tmp_path):
    from darksec import sdr

    path = tmp_path / "rtl.iq"
    # 127/128 is the uint8 midpoint: near-zero complex samples.
    path.write_bytes(bytes([127, 128] * 512))
    blocks = list(sdr._iter_iq(path, sample_format="u8"))
    assert blocks
    assert float(np.max(np.abs(blocks[0]))) < 0.02


def test_rtl_tcp_commands_and_handshake(monkeypatch):
    from darksec import sdr

    class FakeSocket:
        def __init__(self):
            self.header = b"RTL0" + struct.pack(">II", 5, 29)
            self.sent = []
            self.closed = False

        def settimeout(self, _value):
            pass

        def recv(self, count):
            data, self.header = self.header[:count], self.header[count:]
            return data

        def sendall(self, data):
            self.sent.append(data)

        def close(self):
            self.closed = True

    fake = FakeSocket()
    monkeypatch.setattr(sdr.socket, "create_connection", lambda *a, **k: fake)
    client = sdr.RTLTCPClient("127.0.0.1", 1234)
    info = client.connect()
    assert info["tuner_type"] == 5 and info["gain_count"] == 29
    client.set_frequency(100_000_000)
    client.set_sample_rate(1_024_000)
    client.set_gain(40.0)
    client.set_ppm(-12)
    client.set_bias_tee(True)
    assert fake.sent[0] == b"\x01" + struct.pack(">I", 100_000_000)
    assert fake.sent[1] == b"\x02" + struct.pack(">I", 1_024_000)
    assert fake.sent[2] == b"\x04" + struct.pack(">I", 400)
    assert fake.sent[3] == b"\x05" + struct.pack(">I", 0xFFFFFFF4)
    assert fake.sent[4] == b"\x0e" + struct.pack(">I", 1)
    client.close()
    assert fake.closed is True


def test_uconsole_platform_guidance(monkeypatch):
    from darksec import sdr

    monkeypatch.setattr(sdr, "_read_first", lambda paths, default="": "ClockworkPi uConsole")
    monkeypatch.setattr(sdr.shutil, "which", lambda name: None)
    platform = sdr.platform_info()
    assert platform["uconsole"] is True
    assert platform["raspberry_pi"] is False
    tools = {"rtl_test": None, "hackrf_info": None, "SoapySDRUtil": None}
    notes = " ".join(n["text"] for n in sdr._platform_guidance(platform, {}, {}, tools))
    assert "USB-A" in notes
    assert "GPIO 7" in notes
    assert "15–30" in notes


def test_choose_backend_prefers_hackrf():
    from darksec import sdr

    info = {"hackrf": {"connected": True}, "rtl": {"connected": True},
            "rtl_tcp": {"configured": True}}
    original = sdr.detect
    try:
        sdr.detect = lambda force=False: info
        assert sdr.choose_backend("auto") == "hackrf"
        assert sdr.choose_backend("rtl") == "rtl"
        assert sdr.choose_backend("rtl_tcp") == "rtl_tcp"
    finally:
        sdr.detect = original


def test_websdr_tune_url_uses_khz_and_mode():
    from darksec import websdr

    url = websdr.tune_url({"url": "http://example.org:8073/"}, 7_100_000, "lsb")
    assert url == "http://example.org:8073/?f=7100lsb"


def test_websdr_rejects_offline_and_flagged_receivers():
    from darksec import websdr

    base = {"url": "http://good.example:8073", "name": "Good", "loc": "X"}
    assert websdr._normalise(dict(base, offline=True)) is None
    assert websdr._normalise(dict(base, flagged=True)) is None
    assert websdr._normalise(dict(base, status="offline")) is None
    ok = websdr._normalise(dict(base, ext_api=0, bands=[0, 30_000_000]))
    assert ok and ok["api_allowed"] is False and ok["kind"] == "kiwi"


def test_sdr_devices_endpoint_reports_platform_and_backends(client, monkeypatch):
    data = {
        "tools": {"rtl_test": "/usr/bin/rtl_test"},
        "hackrf": {"present": True, "connected": False, "raw": ""},
        "rtl": {"present": True, "connected": True, "tuner": "R820T", "gains": [0, 10]},
        "soapy": {"present": False, "connected": False, "devices": []},
        "rtl_tcp": {"configured": False, "host": "", "port": 1234},
        "platform": {"model": "Raspberry Pi 4", "arch": "aarch64", "raspberry_pi": True},
        "guidance": [{"level": "info", "text": "use a powered hub"}],
        "backends": [{"id": "rtl", "name": "RTL-SDR", "connected": True, "rx": True}],
        "active_backend": "rtl",
        "gps": {"gpspipe": False}, "adsb": {"dump1090": False},
    }
    from darksec import sdr
    from darksec import status

    monkeypatch.setattr(sdr, "detect", lambda force=False: data)
    monkeypatch.setattr(status.sdr, "detect", lambda force=False: data)
    status._hw_cache.update({"ts": 0, "data": None})
    r = client.get("/api/sdr/devices")
    assert r.status_code == 200
    out = r.json()
    assert out["active_backend"] == "rtl"
    assert out["platform"]["raspberry_pi"] is True
    assert out["backends"][0]["id"] == "rtl"


def test_websdr_endpoint_returns_cached_directory(client, monkeypatch):
    from darksec import websdr

    monkeypatch.setattr(websdr, "receivers", lambda **kwargs: {
        "receivers": [{"id": "x", "name": "Test Kiwi", "url": "http://example.org:8073",
                       "loc": "Test", "band": "HF", "kind": "kiwi"}],
        "count": 1, "source": "test", "fetched_at": 1, "error": None,
        "stale": False, "note": "",
    })
    r = client.get("/api/sdr/websdr?q=test&band=HF")
    assert r.status_code == 200
    assert r.json()["receivers"][0]["name"] == "Test Kiwi"


def test_live_preflight_without_radio_is_explicit(monkeypatch):
    from darksec import sdr

    _reset_detect(monkeypatch, {
        "hackrf": {"connected": False}, "rtl": {"connected": False},
        "rtl_tcp": {"configured": False}, "guidance": [{"text": "connect a radio"}],
    })
    try:
        sdr.live_preflight("auto")
    except ValueError as exc:
        assert "No SDR receiver" in str(exc)
    else:
        raise AssertionError("live_preflight should fail without a radio")


def test_live_demod_outputs_pcm_for_am_and_fm():
    from darksec.sdr import _LiveDemod

    rate = 1_024_000
    t = np.arange(int(rate * 0.1)) / rate
    for mode, iq in (
        ("am", (1.0 + 0.4 * np.sin(2 * np.pi * 1000 * t)).astype(np.complex64)),
        ("nfm", np.exp(1j * 2.0 * np.sin(2 * np.pi * 1000 * t)).astype(np.complex64)),
        ("wfm", np.exp(1j * 3.0 * np.sin(2 * np.pi * 1000 * t)).astype(np.complex64)),
    ):
        demod = _LiveDemod(mode, rate)
        pcm = demod.process(iq)
        assert pcm
        samples = np.frombuffer(pcm, dtype="<i2")
        assert samples.size > 100
        assert int(np.max(np.abs(samples))) > 0


def test_live_endpoint_without_radio_returns_clear_400(client, monkeypatch):
    from darksec import sdr

    _reset_detect(monkeypatch, {
        "hackrf": {"connected": False}, "rtl": {"connected": False},
        "rtl_tcp": {"configured": False} if False else {"configured": False},
        "guidance": [{"text": "connect a radio"}],
    })
    # choose_backend/read settings can still look at the real DB; force none.
    monkeypatch.setattr(sdr, "choose_backend", lambda preferred="auto": "none")
    r = client.get("/api/sdr/live", params={"freq_mhz": 100, "mode": "nfm"})
    assert r.status_code == 400
    body = r.json()
    assert "No SDR receiver" in (body.get("detail") or body.get("error") or "")


def test_uconsole_gpio_endpoint_reports_missing_pinctrl(client, monkeypatch):
    import shutil as _shutil

    from darksec import sdr

    monkeypatch.setattr(sdr.shutil, "which", lambda name: None)
    r = client.post("/api/sdr/platform/uconsole-sdr", json={})
    assert r.status_code == 400
    assert "pinctrl" in r.json()["detail"]


def test_rtl_tcp_server_command_uses_bias_tee_flag(monkeypatch):
    from darksec import sdr

    captured = {}

    class Proc:
        pid = 123
        returncode = None

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return Proc()

    monkeypatch.setattr(sdr.shutil, "which", lambda name: "/usr/bin/rtl_tcp" if name == "rtl_tcp" else None)
    monkeypatch.setattr(sdr, "detect", lambda force=False: {"rtl": {"connected": True}})
    monkeypatch.setattr(sdr.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(sdr.time, "sleep", lambda _s: None)
    try:
        sdr.start_rtl_tcp_server(bind="127.0.0.1", port=1234, freq_hz=100e6,
                                 rate=2.4e6, gain=40, ppm=5, bias=True)
        cmd = captured["cmd"]
        assert "-T" in cmd
        assert "-P" in cmd and "5" in cmd
        assert "-a" in cmd and "127.0.0.1" in cmd
    finally:
        sdr.stop_rtl_tcp_server()


def test_rtl_u8_capture_demodulates_and_analyzes(tmp_path):
    import wave

    from darksec import sdr

    rate = 1_024_000
    t = np.arange(int(rate * 0.2)) / rate
    envelope = 0.4 + 0.4 * np.sin(2 * np.pi * 1000 * t)
    i = np.clip(127.5 + envelope * 110, 0, 255).astype(np.uint8)
    q = np.full_like(i, 128, dtype=np.uint8)
    raw = np.empty(i.size * 2, dtype=np.uint8)
    raw[0::2] = i
    raw[1::2] = q
    path = tmp_path / "rtl-am.iq"
    path.write_bytes(raw.tobytes())

    audio = tmp_path / "am.wav"
    sdr.demod_to_wav(path, audio, rate, "am", sample_format="u8")
    with wave.open(str(audio), "rb") as wf:
        assert wf.getnframes() > 1000
    spectrum = sdr.analyze_spectrum(path, rate, sample_format="u8")
    assert spectrum and len(spectrum["bins"]) > 32


def test_sdr_settings_round_trip(client):
    r = client.put("/api/settings", json={
        "sdr_backend": "rtl_tcp",
        "sdr_ppm": 12,
        "sdr_rtl_gain": 32,
        "sdr_rtl_tcp_host": "127.0.0.1",
        "sdr_rtl_tcp_port": 12346,
        "sdr_live_squelch": 15,
        "sdr_live_rate": 1.024,
        "sdr_bias_tee": True,
        "sdr_server_port": 12346,
        "sdr_server_bind": "127.0.0.1",
    })
    assert r.status_code == 200, r.text
    s = client.get("/api/settings").json()
    assert s["sdr_backend"] == "rtl_tcp"
    assert s["sdr_ppm"] == 12
    assert s["sdr_rtl_tcp_port"] == 12346
    assert s["sdr_bias_tee"] is True
    # restore defaults used by the other tests
    client.put("/api/settings", json={
        "sdr_backend": "auto", "sdr_ppm": 0, "sdr_rtl_gain": 40,
        "sdr_rtl_tcp_host": "", "sdr_rtl_tcp_port": 1234,
        "sdr_live_squelch": 0, "sdr_bias_tee": False,
        "sdr_server_port": 1234, "sdr_server_bind": "0.0.0.0",
    })


def test_live_stop_unknown_session_is_safe(client):
    r = client.post("/api/sdr/live/stop?session=missing", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_live_spectrum_frame_shape():
    from darksec.sdr import _LiveSpectrum

    rate = 1_024_000
    t = np.arange(4096) / rate
    iq = np.exp(1j * 2 * np.pi * 25_000 * t).astype(np.complex64)
    frame = _LiveSpectrum().process(iq, 100_000_000.0, rate)
    assert frame["type"] == "spectrum"
    assert len(frame["bins"]) == 512
    assert frame["start_hz"] < 100_000_000 < frame["end_hz"]
    assert frame["peak_hz"] > 0


def test_adsb_manager_status_and_stop_are_safe(client):
    r = client.get("/api/sdr/adsb/status")
    assert r.status_code == 200
    out = r.json()
    assert "running" in out and "binary" in out and "port" in out
    r = client.post("/api/sdr/adsb/stop", json={})
    assert r.status_code == 200
    assert r.json()["running"] is False


def test_aio_v2_rtl_test_is_recognized_as_connected():
    from darksec import sdr

    out = """Found 1 device(s):
  0:  HackerGadgets, AIO_V2 Ext, SN: 25120901

Using device 0: Generic RTL2832U OEM
Found Rafael Micro R820T tuner
Supported gain values (29): 0.0 0.9 1.4 2.7 3.7
Enabled direct sampling mode, input 2
Sampling at 2048000 S/s.
No E4000 tuner found, aborting.
"""
    parsed = sdr._parse_rtl_test(out)
    assert parsed["device_count"] == 1
    assert parsed["device_name"] == "HackerGadgets, AIO_V2 Ext"
    assert parsed["tuner"] == "Rafael Micro R820T"
    assert parsed["direct_sampling"] == 2
    assert parsed["error"] == ""
    assert parsed["sample_rate"] == 2048000


def test_rtl_tcp_direct_sampling_command(monkeypatch):
    from darksec import sdr

    class FakeSocket:
        def __init__(self):
            self.header = b"RTL0" + struct.pack(">II", 5, 29)
            self.sent = []

        def settimeout(self, _value):
            pass

        def recv(self, count):
            data, self.header = self.header[:count], self.header[count:]
            return data

        def sendall(self, data):
            self.sent.append(data)

        def close(self):
            pass

    fake = FakeSocket()
    monkeypatch.setattr(sdr.socket, "create_connection", lambda *a, **k: fake)
    client = sdr.RTLTCPClient("127.0.0.1")
    client.connect()
    client.set_direct_sampling(2)
    assert fake.sent[-1] == b"\x09" + struct.pack(">I", 2)
