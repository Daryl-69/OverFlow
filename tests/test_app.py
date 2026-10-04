import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from upstream.intake.app import create_app
from upstream.intake.whatsapp import WhatsAppConfig, parse_answer
from upstream.reader.synth import PhotoParams, render_photo

SECRET = "app-secret"


class FakeGraph:
    """Stands in for graph.facebook.com and the media CDN."""

    def __init__(self):
        self.sent = []
        self.media = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        path = request.url.path
        if request.method == "POST" and path.endswith("/messages"):
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})
        if request.url.host == "lookaside.example" and request.method == "GET":
            return httpx.Response(200, content=self.media[path.strip("/")])
        if request.method == "GET" and path.startswith("/v21.0/"):
            mid = path.split("/")[-1]
            return httpx.Response(200, json={"url": f"https://lookaside.example/{mid}", "mime_type": "image/jpeg"})
        return httpx.Response(404)


@pytest.fixture(scope="module")
def app_and_graph(tmp_path_factory, scenario_file):
    graph = FakeGraph()
    cfg = WhatsAppConfig(token="test-token", verify_token="verify-me", app_secret=SECRET)
    app = create_app(var_dir=tmp_path_factory.mktemp("var"), scenario_path=scenario_file, whatsapp=cfg,
                     wa_client=httpx.Client(transport=httpx.MockTransport(graph)))
    return app, graph


@pytest.fixture()
def client(app_and_graph):
    app, _ = app_and_graph
    with TestClient(app) as c:
        c.post("/api/live/reset")
        yield c


def test_pages_and_static(client):
    assert client.get("/api/health").json()["service"] == "upstream"
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (302, 307) and r.headers["location"] == "/dashboard/"
    page = client.get("/dashboard/")
    assert page.status_code == 200 and 'data-mode="app"' in page.text
    assert client.get("/dashboard/app.js").status_code == 200
    assert client.get("/dashboard/vendor/leaflet/leaflet.js").status_code == 200
    assert client.get("/dashboard/card.svg").headers["content-type"].startswith("image/svg")
    assert "Upstream first glass" in client.get("/report").text
    sc = client.get("/api/scenario").json()
    assert sc["version"] == 1 and sc["frames"]
    hh = client.get("/api/households").json()
    assert len(hh) == 140 and {"id", "lat", "lon", "role", "arrival_min"} <= set(hh[0])


def test_card_and_qr_endpoints(client):
    svg = client.get("/card/H-0042.svg")
    assert svg.status_code == 200 and "Household H-0042" in svg.text
    png = client.get("/card/H-0042.png?dpi=150")
    assert png.status_code == 200 and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get("/card/bad%20id.svg").status_code == 400
    assert client.get("/api/qr.png", params={"data": "http://x/report"}).content[:4] == b"\x89PNG"
    demo = client.get("/api/demo-photo", params={"household": "H-0003", "turbidity": 40, "chlorine": 0})
    assert demo.status_code == 200 and demo.headers["content-type"] == "image/jpeg"


def test_read_only_endpoint(client):
    photo = render_photo("H-0010", PhotoParams(turbidity=2, chlorine=0.6), seed=1)
    r = client.post("/api/read", files={"photo": ("p.jpg", photo, "image/jpeg")}).json()
    assert r["ok"] and r["household_id"] == "H-0010"
    assert client.get("/api/live").json()["reports"] == []     # nothing stored


def test_photo_submission_levels_and_alert(client):
    sc = client.get("/api/scenario").json()
    hid = sc["truth"]["downstream_households"][0]
    clean = render_photo(hid, PhotoParams(turbidity=1, chlorine=0.6), seed=21)
    r = client.post("/api/photo", files={"photo": ("a.jpg", clean, "image/jpeg")}, data={"answer": "fine"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report"]["household"] == hid and body["level"] in ("green", "amber")
    muddy = render_photo(hid, PhotoParams(turbidity=60, chlorine=0.0), seed=22)
    r = client.post("/api/photo", files={"photo": ("b.jpg", muddy, "image/jpeg")}, data={"answer": "smell"})
    body = r.json()
    assert body["level"] == "red" and body["alert"]
    assert "boiling" in body["message"]
    live = client.get("/api/live").json()
    assert live["alert"] and live["posterior"]["p_breach"] > 0.5
    assert any(a["household"] == hid for a in live["advisories"])
    # re-sending the exact same photo is flagged and kept out of the triangulation
    r = client.post("/api/photo", files={"photo": ("b.jpg", muddy, "image/jpeg")}).json()
    assert "duplicate_photo" in r["report"]["flags"]
    live = client.get("/api/live").json()
    assert [x for x in live["reports"] if x["id"] == r["report"]["id"]][0]["excluded"]
    csv = client.get("/api/evidence.csv")
    assert csv.status_code == 200 and csv.text.splitlines()[0].startswith("report_id,household")
    assert len(csv.text.strip().splitlines()) == 4
    photo_name = client.get("/api/evidence.json").json()["rows"][0]["photo"]
    assert client.get(f"/api/photos/{photo_name}").status_code == 200
    assert client.get("/api/photos/..%2Fupstream.sqlite3").status_code == 404


def test_bad_photos_ask_for_a_retake(client):
    blurry = render_photo("H-0001", PhotoParams(blur=9), seed=3)
    r = client.post("/api/photo", files={"photo": ("x.jpg", blurry, "image/jpeg")})
    assert r.status_code == 422 and any(i.startswith("blurry") for i in r.json()["issues"])
    r = client.post("/api/photo", files={"photo": ("x.jpg", b"nope", "image/jpeg")})
    assert r.status_code == 422
    unknown = render_photo("H-9999", PhotoParams(), seed=4)
    assert client.post("/api/photo", files={"photo": ("x.jpg", unknown, "image/jpeg")}).status_code == 404
    assert client.get("/api/live").json()["reports"] == []


def test_manual_reports_and_answers(client):
    r = client.post("/api/reports", json={"household": "H-0001", "turbidity": 0.08, "chlorine": 0.6})
    assert r.status_code == 200 and r.json()["level"] == "green"
    rid = r.json()["report"]["id"]
    assert client.post(f"/api/reports/{rid}/answer", json={"answer": "smell"}).status_code == 200
    assert client.post(f"/api/reports/{rid}/answer", json={"answer": "maybe"}).status_code == 400
    assert client.post("/api/reports", json={"household": "H-0001", "turbidity": 3}).status_code == 400
    assert client.post("/api/reports", json={"household": "nobody", "turbidity": 0.1}).status_code == 404
    assert client.post("/api/reports", json={"household": "H-0001", "time": "yesterday"}).status_code == 400
    sup = client.post("/api/live/supply", json={}).json()
    assert sup["cycle"] >= 1


def test_whatsapp_flow(client, app_and_graph):
    _, graph = app_and_graph
    graph.sent.clear()
    assert client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                                   "hub.challenge": "42"}).text == "42"
    assert client.get("/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                                   "hub.challenge": "42"}).status_code == 403
    graph.media["MEDIA1"] = render_photo("H-0020", PhotoParams(turbidity=1, chlorine=0.7), seed=31)

    def post(payload):
        raw = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
        return client.post("/whatsapp/webhook", content=raw,
                           headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig})

    def wrap(msg):
        return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": "PN1", "display_phone_number": "1"},
            "messages": [msg]}}]}]}

    assert post(wrap({"from": "919800000001", "id": "w1", "timestamp": "1700000000", "type": "image",
                      "image": {"id": "MEDIA1", "mime_type": "image/jpeg"}})).status_code == 200
    assert graph.sent and graph.sent[-1]["to"] == "919800000001"
    assert "H-0020" in graph.sent[-1]["text"]["body"]
    assert post(wrap({"from": "919800000001", "id": "w2", "timestamp": "1700000060", "type": "text",
                      "text": {"body": "it smells bad"}})).status_code == 200
    assert "noted 'smell'" in graph.sent[-1]["text"]["body"]
    live = client.get("/api/live").json()
    mine = [r for r in live["reports"] if r["household"] == "H-0020"]
    assert mine and mine[-1]["answer"] == "smell" and mine[-1]["source"] == "whatsapp"
    # unsigned or wrongly signed deliveries are refused
    bad = client.post("/whatsapp/webhook", content=b"{}", headers={"X-Hub-Signature-256": "sha256=00"})
    assert bad.status_code == 401
    # the phone number is not stored anywhere in the live state
    assert "919800000001" not in json.dumps(live)


def test_parse_answer():
    assert parse_answer("Water is FINE today") == "fine"
    assert parse_answer("brown colour") == "dirty"
    assert parse_answer("hello") is None
