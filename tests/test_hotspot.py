from jarvis.net import hotspot

BS = "\\"


def test_wifi_qr_payload_format_and_escaping():
    assert hotspot.wifi_qr_payload("jarvis-ab12", "pass") == "WIFI:T:WPA;S:jarvis-ab12;P:pass;;"
    # characters with special meaning in the QR format are escaped so the password survives
    password = 'p;w:d,"x' + BS
    expected = "WIFI:T:WPA;S:a;P:p" + BS + ";w" + BS + ":d" + BS + "," + BS + '"x' + BS + BS + ";;"
    assert hotspot.wifi_qr_payload("a", password) == expected


def test_credentials_are_random_and_strong():
    a, b = hotspot.new_credentials(), hotspot.new_credentials()
    assert a != b and a[0].startswith("jarvis-") and len(a[1]) == 20


def test_hotspot_start_uses_argument_list_not_a_shell():
    calls = []
    hotspot.start("jarvis-x", "pw; rm -rf /", run=lambda argv, **k: calls.append(argv))
    assert calls[0][:4] == ["nmcli", "device", "wifi", "hotspot"]
    assert "pw; rm -rf /" in calls[0]                       # one list item, never interpreted
