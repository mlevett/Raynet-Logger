import importlib
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from fastapi.testclient import TestClient


def make_client(tmp_path: Path):
    os.environ["RAYNET_DB_PATH"] = str(tmp_path / "test.db")
    os.environ["RAYNET_DATA_DIR"] = str(tmp_path)
    os.environ["RAYNET_BACKUP_DIR"] = str(tmp_path / "backups")
    os.environ["RAYNET_BACKUP_INTERVAL_SECONDS"] = "0"
    import app
    importlib.reload(app)
    return TestClient(app.app)


def test_operational_flow(tmp_path):
    with make_client(tmp_path) as client:
        assert client.get("/api/bootstrap-status").json()["needs_setup"] is True
        response = client.post("/api/setup", json={
            "username": "control", "display_name": "Control", "callsign": "M0TST", "password": "correct-horse-battery"
        })
        assert response.status_code == 200
        event = client.post("/api/events", json={
            "name": "Exercise Alpha", "location": "Hampshire", "control_callsign": "CONTROL",
            "event_notes": "Marathon support", "location_details": "Race HQ",
            "event_contacts": "Duty manager: 07000 000000", "radio_frequency": "145.350 MHz",
            "ctcss_tones": "88.5 Hz", "radio_mode": "DMR", "talk_groups": "235",
        }).json()
        assert event["radio_frequency"] == "145.350 MHz"
        assert event["ctcss_tones"] == "88.5 Hz"
        assert event["radio_mode"] == "DMR"
        assert event["talk_groups"] == "235"
        station = client.post(f"/api/events/{event['id']}/stations", json={
            "callsign": "M0ABC", "tactical_call": "TEAM 1", "check_interval_minutes": 30, "on_duty": True, "notes": ""
        }).json()
        with client.websocket_connect(f"/ws/events/{event['id']}") as websocket:
            presence = websocket.receive_json()
            assert presence["type"] == "presence"
            message = client.post(f"/api/events/{event['id']}/messages", json={
                "callsign": "TEAM 1",
                "message": "Arrived at checkpoint", "direction": "received", "priority": "routine"
            })
            assert message.status_code == 200
            live_update = websocket.receive_json()
            assert live_update["type"] == "message_created"
        snapshot = client.get(f"/api/events/{event['id']}/snapshot").json()
        assert snapshot["messages"][0]["sequence"] == 1
        assert snapshot["messages"][0]["station_callsign"] == "M0ABC"
        assert snapshot["messages"][0]["tactical_call"] == "TEAM 1"
        assert snapshot["messages"][0]["callsign"] == "M0ABC / TEAM 1"
        assert snapshot["messages"][0]["address_mode"] == "both"
        assert snapshot["stations"][0]["last_heard_at"] is not None
        unknown = client.post(f"/api/events/{event['id']}/messages", json={
            "callsign": "UNKNOWN", "message": "Test", "direction": "received", "priority": "routine"
        })
        assert unknown.status_code == 409
        export = client.get(f"/api/events/{event['id']}/export/csv")
        assert export.status_code == 200
        assert b"Arrived at checkpoint" in export.content
        selected = client.get(f"/api/events/{event['id']}/export/json?sections=summary,operators").json()
        assert selected["event"]["name"] == "Exercise Alpha"
        assert selected["stations"][0]["callsign"] == "M0ABC"
        assert "messages" not in selected
        pdf = client.get(f"/api/events/{event['id']}/export/pdf?sections=summary,messages,operators,audit")
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content.startswith(b"%PDF")


def test_identity_change_is_audited_and_logged(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/setup", json={
            "username": "control", "display_name": "Control", "callsign": "M0TST", "password": "correct-horse-battery"
        })
        event = client.post("/api/events", json={"name": "Exercise Bravo", "location": "Hampshire", "control_callsign": "CONTROL"}).json()
        station = client.post(f"/api/events/{event['id']}/stations", json={
            "callsign": "G1SEH", "tactical_call": "CONTROL", "check_interval_minutes": 30, "on_duty": True, "notes": ""
        }).json()
        changed = client.patch(f"/api/stations/{station['id']}", json={"callsign": "M0CTL", "tactical_call": "NET CONTROL"})
        assert changed.status_code == 200
        snapshot = client.get(f"/api/events/{event['id']}/snapshot").json()
        assert len(snapshot["messages"]) == 1
        system_line = snapshot["messages"][0]
        assert system_line["direction"] == "info"
        assert system_line["station_callsign"] == "M0CTL"
        assert system_line["tactical_call"] == "NET CONTROL"
        assert "G1SEH / CONTROL" in system_line["message"]
        assert "M0CTL / NET CONTROL" in system_line["message"]
        audit = client.get(f"/api/events/{event['id']}/audit").json()
        identity_entries = [item for item in audit if item["action"] == "identity_change"]
        assert len(identity_entries) == 1
        assert '"callsign":"G1SEH"' in identity_entries[0]["before_json"]
        assert '"callsign":"M0CTL"' in identity_entries[0]["after_json"]


def test_unified_identity_matches_callsign_or_tactical(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/setup", json={
            "username": "control", "display_name": "Control", "callsign": "M0TST", "password": "correct-horse-battery"
        })
        event = client.post("/api/events", json={"name": "Exercise Charlie", "location": "Hampshire", "control_callsign": "CONTROL"}).json()
        client.post(f"/api/events/{event['id']}/stations", json={
            "callsign": "G1SEH", "tactical_call": "CONTROL", "check_interval_minutes": 30, "on_duty": True, "notes": ""
        })

        by_tactical = client.post(f"/api/events/{event['id']}/messages", json={
            "callsign": "CONTROL", "message": "Tactical match", "direction": "received", "priority": "routine"
        })
        assert by_tactical.status_code == 200
        tactical_item = by_tactical.json()["message"]
        assert tactical_item["callsign"] == "G1SEH / CONTROL"
        assert tactical_item["station_callsign"] == "G1SEH"
        assert tactical_item["tactical_call"] == "CONTROL"
        assert tactical_item["address_mode"] == "both"

        by_callsign = client.post(f"/api/events/{event['id']}/messages", json={
            "callsign": "G1SEH", "message": "Callsign match", "direction": "received", "priority": "routine"
        })
        assert by_callsign.status_code == 200
        callsign_item = by_callsign.json()["message"]
        assert callsign_item["callsign"] == "G1SEH / CONTROL"
        assert callsign_item["station_callsign"] == "G1SEH"
        assert callsign_item["tactical_call"] == "CONTROL"
        assert callsign_item["address_mode"] == "both"


def test_frontend_has_unified_identity_and_sound_defaults_on():
    base = BASE_DIR
    js = (base / "static" / "app.js").read_text()
    html = (base / "static" / "index.html").read_text()

    assert "localStorage.getItem(SOUND_PREF_KEY) !== 'muted'" in js
    assert "const SOUND_PREF_KEY = 'raynet-alert-sound-v2'" in js
    assert "identity_mode: 'both'" in js
    assert "option.callsign} / ${option.tactical" in js
    assert 'id="identity-mode-picker"' not in html
    assert "Each result shows both identities" in html
    assert "KeyR: 'received'" in js
    assert 'aria-keyshortcuts="Alt+R"' in html
    assert "tactical.value = 'CONTROL'" in js
    assert "Create event" in js and "1 of 2" in js
    assert "Assign operators" in js and "2 of 2" in js
    assert "Add operator" in js
    assert "wizard-guest-name" in js
    assert "forceOpen || input.value || state.callsignOptions.length" not in js
    assert 'id="menu-administration-button" class="permission-admin"' in html
    assert 'data-tab="admin"' not in html
    assert 'data-tab="users"' not in html


def test_in_control_operator_is_persisted_and_welfare_exempt(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/setup", json={
            "username": "control", "display_name": "Control", "callsign": "M0TST", "password": "correct-horse-battery"
        })
        event = client.post("/api/events", json={"name": "Exercise Delta", "location": "Hampshire", "control_callsign": "CONTROL"}).json()
        station = client.post(f"/api/events/{event['id']}/stations", json={
            "callsign": "M0CTL", "tactical_call": "CONTROL 2", "check_interval_minutes": 30,
            "on_duty": True, "in_control": True, "notes": "At control"
        }).json()
        assert station["in_control"] == 1

        changed = client.patch(f"/api/stations/{station['id']}", json={"in_control": False})
        assert changed.status_code == 200
        assert changed.json()["in_control"] == 0

        stood_down = client.patch(f"/api/stations/{station['id']}", json={"duty_status": "stood_down"})
        assert stood_down.status_code == 200
        assert stood_down.json()["duty_status"] == "stood_down"
        assert stood_down.json()["on_duty"] == 0

        returned = client.patch(f"/api/stations/{station['id']}", json={"duty_status": "on_duty"})
        assert returned.status_code == 200
        assert returned.json()["duty_status"] == "on_duty"
        assert returned.json()["on_duty"] == 1

    js = (BASE_DIR / "static" / "app.js").read_text()
    assert "if (station.in_control) return {state: 'control'" in js


def test_control_user_logs_event_control_callsign(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/setup", json={
            "username": "control", "display_name": "Mathew", "callsign": "M0NFZ", "password": "correct-horse-battery"
        })
        user_id = client.get("/api/users").json()[0]["id"]
        event = client.post("/api/events", json={
            "name": "Exercise Echo", "location": "Hampshire", "control_callsign": "RAYNET CONTROL"
        }).json()
        assignment = client.post(f"/api/events/{event['id']}/operators", json={
            "user_id": user_id, "in_control": True, "tactical_call": "RAYNET CONTROL", "check_interval_minutes": 45
        })
        assert assignment.status_code == 200
        assert assignment.json()["in_control"] == 1
        snapshot = client.get(f"/api/events/{event['id']}/snapshot").json()
        assert snapshot["stations"][0]["check_interval_minutes"] == 45
        message = client.post(f"/api/events/{event['id']}/messages", json={
            "callsign": "UNKNOWN", "message": "Control test", "direction": "sent", "priority": "routine",
            "force_unknown": True
        })
        assert message.status_code == 200
        logged = message.json()["message"]
        assert logged["operator_name"] == "Mathew"
        assert logged["operator_callsign"] == "M0NFZ"
        assert logged["operator_control_call"] == "RAYNET CONTROL"


def test_permanent_user_auto_joins_event_as_control(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/setup", json={
            "username": "control", "display_name": "Mathew", "callsign": "M0NFZ", "password": "correct-horse-battery"
        })
        event = client.post("/api/events", json={
            "name": "Exercise Foxtrot", "location": "Hampshire", "control_callsign": "G1SEH"
        }).json()
        joined = client.post(f"/api/events/{event['id']}/join")
        assert joined.status_code == 200
        assert joined.json() == {"assignment_created": True, "station_created": True}
        snapshot = client.get(f"/api/events/{event['id']}/snapshot").json()
        assert snapshot["event_users"][0]["tactical_call"] == ""
        assert snapshot["event_users"][0]["in_control"] == 0
        assert snapshot["stations"][0]["callsign"] == "M0NFZ"
        assert snapshot["stations"][0]["tactical_call"] == ""
        assert snapshot["stations"][0]["in_control"] == 0

        joined_again = client.post(f"/api/events/{event['id']}/join")
        assert joined_again.json() == {"assignment_created": False, "station_created": False}


def test_permanent_user_can_be_unassigned_without_deleting_account_or_messages(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/setup", json={
            "username": "control", "display_name": "Mathew", "callsign": "M0NFZ", "password": "correct-horse-battery"
        })
        user_id = client.get("/api/users").json()[0]["id"]
        event = client.post("/api/events", json={
            "name": "Exercise Unassign", "location": "", "control_callsign": "CONTROL"
        }).json()
        assignment = client.post(f"/api/events/{event['id']}/operators", json={
            "user_id": user_id, "in_control": False, "tactical_call": "CP1", "check_interval_minutes": 30
        }).json()
        snapshot = client.get(f"/api/events/{event['id']}/snapshot").json()
        station = snapshot["stations"][0]
        message = client.post(f"/api/events/{event['id']}/messages", json={
            "station_id": station["id"], "callsign": "CP1", "message": "Radio check",
            "direction": "received", "priority": "routine"
        })
        assert message.status_code == 200

        removed = client.delete(f"/api/event-operators/{assignment['id']}")
        assert removed.status_code == 200
        assert removed.json()["station_id"] == station["id"]
        after = client.get(f"/api/events/{event['id']}/snapshot").json()
        assert after["event_users"] == []
        assert after["stations"] == []
        assert len(after["messages"]) == 1
        assert after["messages"][0]["station_callsign"] == "M0NFZ"
        assert any(item["id"] == user_id for item in client.get("/api/users").json())


def test_non_account_operator_removal_cleans_roster_and_preserves_log(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/setup", json={
            "username": "control", "display_name": "Control", "callsign": "M0TST", "password": "correct-horse-battery"
        })
        event = client.post("/api/events", json={
            "name": "Exercise Remove", "location": "", "control_callsign": "CONTROL"
        }).json()
        station = client.post(f"/api/events/{event['id']}/stations", json={
            "name": "Guest Operator", "callsign": "M0GST", "tactical_call": "CP2",
            "check_interval_minutes": 30, "on_duty": True, "notes": "Event operator"
        }).json()
        logged = client.post(f"/api/events/{event['id']}/messages", json={
            "station_id": station["id"], "callsign": "CP2", "message": "Checkpoint ready",
            "direction": "received", "priority": "routine"
        })
        assert logged.status_code == 200

        removed = client.delete(f"/api/stations/{station['id']}")
        assert removed.status_code == 200
        after = client.get(f"/api/events/{event['id']}/snapshot").json()
        assert after["stations"] == []
        assert after["event_users"] == []
        assert len(after["messages"]) == 1
        assert after["messages"][0]["station_id"] is None
        assert after["messages"][0]["station_callsign"] == "M0GST"
        assert after["messages"][0]["tactical_call"] == "CP2"
        audit_rows = client.get(f"/api/events/{event['id']}/audit").json()
        deletion = next(
            item for item in audit_rows
            if item["entity_type"] == "station" and item["entity_id"] == station["id"] and item["action"] == "delete"
        )
        assert deletion["before_json"] is not None


def test_control_callsign_presets_can_be_managed(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/setup", json={
            "username": "control", "display_name": "Control", "callsign": "M0TST", "password": "correct-horse-battery"
        })
        defaults = client.get("/api/control-callsigns").json()
        assert {item["callsign"] for item in defaults} == {"CONTROL", "RAYNET CONTROL"}
        created = client.post("/api/control-callsigns", json={"callsign": "g1seh"})
        assert created.status_code == 200
        assert created.json()["callsign"] == "G1SEH"
        edited = client.patch(f"/api/control-callsigns/{created.json()['id']}", json={"callsign": "G0SEH"})
        assert edited.status_code == 200
        assert edited.json()["callsign"] == "G0SEH"
        removed = client.delete(f"/api/control-callsigns/{created.json()['id']}")
        assert removed.status_code == 200
        assert all(item["callsign"] != "G1SEH" for item in client.get("/api/control-callsigns").json())


def test_all_stations_broadcast_does_not_require_roster_entry(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/setup", json={
            "username": "control", "display_name": "Control", "callsign": "M0TST", "password": "correct-horse-battery"
        })
        event = client.post("/api/events", json={"name": "Exercise Golf", "location": "Hampshire", "control_callsign": "CONTROL"}).json()
        response = client.post(f"/api/events/{event['id']}/messages", json={
            "callsign": "ALL STATIONS", "message": "Stand by", "direction": "sent", "priority": "priority"
        })
        assert response.status_code == 200
        assert response.json()["message"]["callsign"] == "ALL STATIONS"


def test_admin_can_update_global_branding(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/setup", json={
            "username": "control", "display_name": "Control", "callsign": "M0TST", "password": "correct-horse-battery"
        })
        logo = "data:image/png;base64,iVBORw0KGgo="
        response = client.put("/api/branding", json={
            "org_name": "South East RAYNET Logger", "tagline": "Operational communications", "logo_data_url": logo
        })
        assert response.status_code == 200
        assert response.json()["org_name"] == "South East RAYNET Logger"
        assert response.json()["logo_data_url"] == logo
        bootstrap = client.get("/api/bootstrap-status").json()
        assert bootstrap["org_name"] == "South East RAYNET Logger"
        assert bootstrap["branding"]["tagline"] == "Operational communications"


def test_admin_can_delete_event_and_related_records(tmp_path):
    with make_client(tmp_path) as client:
        client.post("/api/setup", json={
            "username": "control", "display_name": "Control", "callsign": "M0TST", "password": "correct-horse-battery"
        })
        event = client.post("/api/events", json={"name": "Disposable Test Event"}).json()
        station = client.post(f"/api/events/{event['id']}/stations", json={
            "callsign": "M0DEL", "tactical_call": "CP1", "check_interval_minutes": 30,
            "on_duty": True, "in_control": False,
        })
        assert station.status_code == 200
        message = client.post(f"/api/events/{event['id']}/messages", json={
            "callsign": "M0DEL", "message": "Test traffic", "direction": "received", "priority": "routine"
        })
        assert message.status_code == 200

        deleted = client.delete(f"/api/events/{event['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert deleted.json()["records"]["messages"] == 1
        assert deleted.json()["records"]["stations"] == 1
        assert all(item["id"] != event["id"] for item in client.get("/api/events").json())
        assert client.get(f"/api/events/{event['id']}/snapshot").status_code == 404
