"""shturman-ko-analyst <-> Штурман integration against the real server: КО chat archive
(server/ko/archive.js), the agent API (/api/agent/ko/*, KO_ANALYST_API_KEY), proposals
(server/ko/proposals.js) and their review in the dashboard (/api/ko/*, session + CSRF).

Telegram and Anthropic are local fakes; the fake Anthropic server records every call so the tests
can prove the КО channel makes none with TELEGRAM_ENGINEERING_AI_ENABLED=false, and the fake
Telegram records every outbound call so the tests can prove nothing is ever sent to the КО chat.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from conftest import ShturmanServer

SECRET = "wh-secret-test"
BOTKEY = "tg-bot-test-key"
ENG_CHAT = "-1001234567890"
OWNER_CHAT = "1000"
KO_KEY = "ko-analyst-key-0123456789abcdef"
AGENT_KEY = "secret-test-key"
USER, PASSWORD = "operator", "pass-word-123"

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
NOT_IMAGE = b"MZ\x90\x00this is not an image at all"


def fake_telegram(files):
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            method = self.path.rsplit("/", 1)[-1]
            calls.append((method, body))
            if method == "getFile":
                return self._json({"ok": True, "result": {"file_id": body["file_id"], "file_path": f"f/{body['file_id']}"}})
            return self._json({"ok": True, "result": {"message_id": 1}})

        def do_GET(self):
            fid = self.path.rsplit("/", 1)[-1]
            if "/file/bot" in self.path and fid in files:
                raw = files[fid]
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.calls = calls
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def fake_anthropic():
    calls = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            calls.append(self.path)
            raw = b'{"error":"must not be called"}'
            self.send_response(500)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.calls = calls
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def env(tmp_path):
    files = {"p1": JPEG, "p2": PNG, "bad": NOT_IMAGE, "big": JPEG}
    tg, ai = fake_telegram(files), fake_anthropic()
    servers = []

    def start(**extra):
        e = {"TELEGRAM_BOT_TOKEN": BOTKEY, "TELEGRAM_WEBHOOK_SECRET": SECRET, "TELEGRAM_OWNER_CHAT_ID": OWNER_CHAT,
             "TELEGRAM_OWNER_USER_ID": OWNER_CHAT, "TELEGRAM_ENGINEERING_CHAT_ID": ENG_CHAT,
             "TELEGRAM_API_BASE_URL": f"http://127.0.0.1:{tg.server_address[1]}",
             "ANTHROPIC_API_KEY": "sk-test", "ANTHROPIC_API_BASE_URL": f"http://127.0.0.1:{ai.server_address[1]}",
             "TELEGRAM_ENGINEERING_AI_ENABLED": "false", "KO_ANALYST_API_KEY": KO_KEY,
             "DASHBOARD_USERNAME": USER, "DASHBOARD_PASSWORD": PASSWORD, "DASHBOARD_SESSION_SECRET": "s" * 40,
             "KO_MAX_FILE_BYTES": "1000"}
        e.update(extra)
        srv = ShturmanServer(tmp_path / f"srv{len(servers)}", key=AGENT_KEY, seed_demo=True, extra_env=e).start()
        srv.tg, srv.ai = tg, ai
        servers.append(srv)
        return srv

    yield start
    for s in servers:
        s.stop()
    tg.shutdown()
    ai.shutdown()


def msg(message_id, text=None, update_id=None, photo=None, reply_to=None, user=555, caption=None, document=None,
        media_group_id=None, chat=ENG_CHAT):
    m = {"message_id": message_id, "date": 1795000000 + message_id, "chat": {"id": int(chat), "type": "supergroup"},
         "from": {"id": user, "first_name": "Иван", "last_name": "Петров", "username": "ipetrov"}}
    if text is not None:
        m["text"] = text
    if caption is not None:
        m["caption"] = caption
    if photo:
        fid, size = photo if isinstance(photo, tuple) else (photo, 50)
        m["photo"] = [{"file_id": fid + "-s", "file_unique_id": "u-" + fid + "-s", "width": 90, "height": 90, "file_size": 10},
                      {"file_id": fid, "file_unique_id": "u-" + fid, "width": 800, "height": 600, "file_size": size}]
    if document:
        m["document"] = document
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    if media_group_id:
        m["media_group_id"] = media_group_id
    return {"update_id": update_id or message_id * 7, "message": m}


def hook(srv, upd):
    r = requests.post(srv.url + "/api/telegram/webhook", json=upd,
                      headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}, timeout=20)
    assert r.status_code == 200, r.text
    return r


def ko(srv, path, method="GET", key=KO_KEY, **kw):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return requests.request(method, srv.url + "/api/agent/ko" + path, headers=headers, timeout=20, **kw)


def session(srv):
    s = requests.Session()
    assert s.post(srv.url + "/api/login", json={"username": USER, "password": PASSWORD}, timeout=10).status_code == 200
    s.headers["X-CSRF-Token"] = s.get(srv.url + "/api/whoami", timeout=10).json()["csrf_token"]
    return s


def messages(srv, after=0):
    r = ko(srv, f"/messages?after={after}")
    assert r.status_code == 200, r.text
    return r.json()


def project(srv, s, pid):
    return s.get(srv.url + f"/api/projects/{pid}", timeout=10).json()


def find_project(srv, make, product):
    lst = ko(srv, "/projects").json()["projects"]
    return [p for p in lst if p["manufacturer"] == make and p["product_type"] == product][0]


def ev(mid, quote):
    return {"chat_id": ENG_CHAT, "message_id": mid, "quote": quote}


def stage_proposal(pid, to, frm, mid, quote, **kw):
    p = {"kind": "stage_change", "project_ref": {"project_id": pid}, "change": {"field": "status", "from": frm, "to": to},
         "summary": "Образец установлен", "evidence": [ev(mid, quote)], "rationale": "явно сказано в чате",
         "confidence": "high", "requires_approval": True}
    p.update(kw)
    return p


def submit(srv, proposals, key=KO_KEY):
    return ko(srv, "/proposals", method="POST", key=key, json={"proposals": proposals})


def no_outbound_to_ko(srv):
    return [c for c in srv.tg.calls if str((c[1] or {}).get("chat_id")) == ENG_CHAT or c[0].startswith("send")]


# ------------------------------------------------------------------ archive


def test_archive_keeps_text_author_date_ids_order_and_replies_without_ai(env):
    srv = env()
    hook(srv, msg(100, "Поставили образец защиты радиатора на Jolion, встал нормально"))
    hook(srv, msg(101, caption="вид снизу", photo="p1", reply_to=100))
    hook(srv, msg(102, "отлично", reply_to=101))
    page = messages(srv)
    m = page["messages"]
    assert [x["message_id"] for x in m] == [100, 101, 102]
    assert [x["seq"] for x in m] == [1, 2, 3] and page["last_seq"] == 3 and page["has_more"] is False
    first = m[0]
    assert first["text"].startswith("Поставили") and first["from"] == {"id": "555", "name": "Иван Петров", "username": "ipetrov", "is_bot": False}
    assert first["date"] == "2026-11-18T11:08:20.000Z"
    assert m[1]["reply_to_message_id"] == 100 and m[1]["thread_root_id"] == 100
    assert m[2]["thread_root_id"] == 100, "the root of a reply chain is resolved from the archive"
    assert m[1]["photos"][0]["status"] == "stored" and m[1]["photos"][0]["mime_type"] == "image/jpeg"
    # cursor paging
    assert [x["message_id"] for x in messages(srv, after=2)["messages"]] == [102]
    assert ko(srv, "/messages?after=0&limit=1").json()["has_more"] is True
    # no paid AI, nothing sent, nothing queued, existing projects untouched
    assert srv.ai.calls == []
    assert no_outbound_to_ko(srv) == []
    s = session(srv)
    assert s.get(srv.url + "/api/review-queue", timeout=10).json() == []
    log = s.get(srv.url + "/api/telegram/log?channel=engineering", timeout=10).json()["entries"]
    assert {e["outcome"] for e in log} == {"archived"}
    st = s.get(srv.url + "/api/telegram/status", timeout=10).json()["engineering"]
    assert st["ai_enabled"] is False and st["ko_archive"]["messages"] == 3


def test_archive_is_idempotent_for_redelivery_and_duplicate_photos(env):
    srv = env()
    hook(srv, msg(200, caption="замер", photo="p1", update_id=9001))
    hook(srv, msg(200, caption="замер", photo="p1", update_id=9001))   # Telegram retry (same update_id)
    hook(srv, msg(200, caption="замер", photo="p1", update_id=9002))   # same message, new update_id
    hook(srv, msg(201, caption="ещё раз то же фото", photo="p1"))       # same picture forwarded again
    m = messages(srv)["messages"]
    assert [x["message_id"] for x in m] == [200, 201]
    assert m[0]["photos"][0]["file_id"] == m[1]["photos"][0]["file_id"], "same file_unique_id -> one stored file"
    stored = list((srv.data_dir / "ko-files").rglob("*.jpg"))
    assert len(stored) == 1
    getfile = [c for c in srv.tg.calls if c[0] == "getFile"]
    assert len(getfile) == 1, "the picture is downloaded once"


def test_file_type_and_size_limits_and_files_are_not_public(env):
    srv = env()
    hook(srv, msg(300, caption="не картинка", photo="bad"))
    hook(srv, msg(301, caption="слишком большой", photo=("big", 5000)))
    hook(srv, msg(302, caption="pdf", document={"file_id": "doc1", "file_unique_id": "u-doc1", "mime_type": "application/pdf", "file_size": 10}))
    hook(srv, msg(303, caption="png документом", document={"file_id": "p2", "file_unique_id": "u-p2", "mime_type": "image/png", "file_size": 72}))
    m = {x["message_id"]: x for x in messages(srv)["messages"]}
    assert m[300]["photos"][0]["status"] == "rejected" and "JPEG" in m[300]["photos"][0]["error"]
    assert m[301]["photos"][0]["status"] == "rejected" and "лимита" in m[301]["photos"][0]["error"]
    # a non-image document is not downloaded at all: metadata only, no ko_files entry
    assert m[302]["photos"] == [] and m[302]["content_type"] == "document"
    assert m[302]["media"][0]["download"] == "metadata_only" and m[302]["media"][0]["mime_type"] == "application/pdf"
    ok = m[303]["photos"][0]
    assert ok["status"] == "stored" and ok["mime_type"] == "image/png"
    assert not [c for c in srv.tg.calls if c[0] == "getFile" and c[1]["file_id"] in ("big", "doc1")], "pre-checked, never downloaded"
    # bytes only through authenticated routes
    r = ko(srv, f"/files/{ok['file_id']}")
    assert r.status_code == 200 and r.content == PNG
    assert r.headers["X-Content-Type-Options"] == "nosniff" and "attachment" in r.headers["Content-Disposition"]
    assert ko(srv, f"/files/{ok['file_id']}", key=None).status_code in (401, 501)
    assert requests.get(srv.url + f"/api/ko/files/{ok['file_id']}", timeout=10).status_code == 401
    assert session(srv).get(srv.url + f"/api/ko/files/{ok['file_id']}", timeout=10).status_code == 200
    stored = next((srv.data_dir / "ko-files").rglob("*.png"))
    assert not str(stored).startswith(str(srv.uploads)), "КО files live outside the static /uploads mount"
    rel = stored.relative_to(srv.data_dir / "ko-files").as_posix()
    for url in (f"/uploads/{rel}", f"/uploads/../data/ko-files/{rel}", f"/ko-files/{rel}"):
        got = requests.get(srv.url + url, timeout=10, allow_redirects=False)
        assert got.status_code in (302, 401, 404) and got.content != PNG, url   # 302 = redirect to the login page
        got = session(srv).get(srv.url + url, timeout=10, allow_redirects=False)
        assert got.status_code == 404 and got.content != PNG, f"not served statically even with a session: {url}"
    assert ko(srv, "/files/..%2F..%2Fshturman.json").status_code == 404
    assert ko(srv, "/files/" + rel.replace("/", "%2F")).status_code == 404


def test_ai_flag_unset_keeps_previous_behaviour_and_still_archives(env):
    srv = env(TELEGRAM_ENGINEERING_AI_ENABLED="")
    hook(srv, msg(400, "что-то про проект"))
    assert len(srv.ai.calls) == 1, "unset flag = the channel behaves exactly as before"
    assert [x["message_id"] for x in messages(srv)["messages"]] == [400]
    assert no_outbound_to_ko(srv) == []


# ------------------------------------------------------------------ access control


def test_ko_api_credentials_are_scoped(env):
    srv = env()
    assert ko(srv, "/messages", key=None).status_code == 401
    assert ko(srv, "/messages", key="wrong-key-wrong-key-wrong").status_code == 401
    assert ko(srv, "/messages", key=AGENT_KEY).status_code == 401, "AGENT_API_KEY does not open the KO API"
    # the KO key opens nothing else
    h = {"Authorization": f"Bearer {KO_KEY}"}
    assert requests.get(srv.url + "/api/agent/projects", headers=h, timeout=10).status_code == 401
    assert requests.post(srv.url + "/api/agent/projects", headers=h, json={}, timeout=10).status_code == 401
    assert requests.post(srv.url + "/api/agent/review-queue", headers=h, json={"kind": "x", "payload": {}}, timeout=10).status_code == 401
    assert requests.get(srv.url + "/api/projects", headers=h, timeout=10).status_code == 401
    assert requests.get(srv.url + "/api/ko/proposals", headers=h, timeout=10).status_code == 401
    assert requests.post(srv.url + "/api/ko/proposals/x/apply", headers=h, json={}, timeout=10).status_code == 401
    # nothing else exists under the KO prefix
    for method, path in (("POST", "/messages"), ("PATCH", "/proposals"), ("DELETE", "/proposals"), ("POST", "/projects"),
                         ("POST", "/proposals/abc/apply"), ("GET", "/stage-transitions")):
        assert ko(srv, path, method=method, json={}).status_code == 404, (method, path)


def test_ko_api_disabled_without_key_or_with_a_reused_key(env):
    assert ko(env(KO_ANALYST_API_KEY=""), "/messages").status_code == 501
    assert ko(env(KO_ANALYST_API_KEY=AGENT_KEY + "-and-long-enough"), "/messages").status_code == 401
    same = env(KO_ANALYST_API_KEY="same-key-for-both-0123456789", AGENT_API_KEY="same-key-for-both-0123456789")
    assert ko(same, "/messages", key="same-key-for-both-0123456789").status_code == 501
    assert ko(env(KO_ANALYST_API_KEY="short"), "/messages", key="short").status_code == 501


# ------------------------------------------------------------------ proposals


def test_proposal_validation_ties_evidence_to_the_archive(env):
    srv = env()
    hook(srv, msg(500, "Поставили образец защиты радиатора на Jolion"))
    pid = find_project(srv, "HAVAL", "Защита радиатора")["id"]
    bad = [
        stage_proposal(pid, "prototype", None, 999, "нет такого"),                                   # unknown message
        stage_proposal(pid, "prototype", None, 500, "придуманная цитата"),                           # quote not in text
        dict(stage_proposal(pid, "prototype", None, 500, "образец"), evidence=[{"chat_id": "-42", "message_id": 500, "quote": "образец"}]),
        dict(stage_proposal(pid, "prototype", None, 500, "образец"), change={"field": "priority", "to": "critical"}),
        stage_proposal(pid, "flying", None, 500, "образец"),
        dict(stage_proposal(pid, "prototype", None, 500, "образец"), apply_now=True),                # unknown field
        dict(stage_proposal(pid, "prototype", None, 500, "образец"), confidence="low"),
        {"kind": "photo_link", "project_ref": {"project_id": pid}, "summary": "s", "rationale": "r", "confidence": "high",
         "evidence": [ev(500, "образец")], "photos": [{"file_id": "not-in-archive"}]},
        {"kind": "deadline_change", "project_ref": {"project_id": pid}, "change": {"to": "2026-02-30"}, "summary": "s",
         "rationale": "r", "confidence": "high", "evidence": [ev(500, "образец")]},
        {"kind": "delete_project", "project_ref": {"project_id": pid}, "summary": "s", "rationale": "r", "confidence": "high",
         "evidence": [ev(500, "образец")]},
    ]
    for p in bad:
        r = submit(srv, [p])
        assert r.status_code == 400, (p, r.text)
    assert submit(srv, [stage_proposal(pid, "prototype", None, 500, "образец")] * 101).status_code == 400
    assert session(srv).get(srv.url + "/api/ko/proposals", timeout=10).json()["proposals"] == []


def test_proposals_are_idempotent_and_never_change_projects(env):
    srv = env()
    hook(srv, msg(600, "Поставили образец защиты радиатора на Jolion"))
    hook(srv, msg(601, "Образец стоит, всё ок"))
    s = session(srv)
    before = s.get(srv.url + "/api/projects", timeout=10).json()
    p = find_project(srv, "HAVAL", "Защита радиатора")
    prop = stage_proposal(p["id"], "prototype", p["status"], 600, "поставили образец", dedupe_key="agent-chosen-key")
    r1 = submit(srv, [prop])
    assert r1.status_code == 201 and r1.json()["counts"] == {"created": 1}
    key = r1.json()["results"][0]["dedupe_key"]
    assert key.startswith("ko:v1:") and key != "agent-chosen-key", "the key is computed by the server"
    assert submit(srv, [prop]).json()["counts"] == {"duplicate": 1}
    # the same fact referenced by make/model/product and with one more source message -> same item, merged evidence
    prop2 = dict(prop, project_ref={"manufacturer": "haval", "vehicle_model": "JOLION", "product_type": "Защита радиатора"},
                 evidence=[ev(600, "поставили образец"), ev(601, "образец стоит")])
    r3 = submit(srv, [prop2])
    assert r3.json()["counts"] == {"updated": 1} and r3.json()["results"][0]["dedupe_key"] == key
    lst = ko(srv, "/proposals").json()["proposals"]
    assert len(lst) == 1
    pl = lst[0]["payload"]
    assert [e["message_id"] for e in pl["evidence"]] == [600, 601]
    assert pl["evidence"][0]["author"] == "Иван Петров" and pl["evidence"][0]["link"] == "https://t.me/c/1234567890/600"
    assert pl["requires_approval"] is True and pl["resolved_project_id"] == p["id"]
    after = s.get(srv.url + "/api/projects", timeout=10).json()
    assert after == before, "submitting proposals never changes a project"


# ------------------------------------------------------------------ applying


def _open(srv, s):
    return s.get(srv.url + "/api/ko/proposals", timeout=10).json()["proposals"]


def test_apply_requires_session_csrf_and_current_version_then_uses_kanban_rules(env):
    srv = env()
    hook(srv, msg(700, "Поставили образец защиты радиатора на Jolion"))
    p = find_project(srv, "HAVAL", "Защита радиатора")
    submit(srv, [stage_proposal(p["id"], "prototype", p["status"], 700, "образец")])
    s = session(srv)
    item = _open(srv, s)[0]
    url = srv.url + f"/api/ko/proposals/{item['id']}/apply"
    assert requests.post(url, json={"version": p["version"]}, timeout=10).status_code == 401           # no session
    no_csrf = requests.Session()
    no_csrf.post(srv.url + "/api/login", json={"username": USER, "password": PASSWORD}, timeout=10)
    assert no_csrf.post(url, json={"version": p["version"]}, timeout=10).status_code == 403            # no CSRF
    assert ko(srv, f"/proposals/{item['id']}/apply", method="POST", json={}).status_code == 404        # agent cannot apply
    assert s.post(url, json={}, timeout=10).status_code == 400                                         # version required
    assert s.post(url, json={"version": p["version"] + 5}, timeout=10).status_code == 409             # stale version
    assert project(srv, s, p["id"])["status"] == p["status"], "nothing applied by failed attempts"
    r = s.post(url, json={"version": p["version"]}, timeout=10)
    assert r.status_code == 200, r.text
    after = project(srv, s, p["id"])
    assert after["status"] == "prototype" and after["version"] == p["version"] + 1
    hist = after["history"]
    tr = [h for h in hist if h["action"] == "stage_transition"][-1]
    assert tr["source"] == "manual" and tr["actor"] == USER and tr["new_value"] == "prototype"
    assert any(h["action"] == "ko_proposal_applied" for h in hist)
    assert s.post(url, json={"version": after["version"]}, timeout=10).status_code == 409, "cannot apply twice"
    resolved = s.get(srv.url + "/api/ko/proposals?status=resolved", timeout=10).json()["proposals"][0]
    assert resolved["resolution"] == "applied" and resolved["payload"]["decision"]["by"] == USER
    assert no_outbound_to_ko(srv) == [] and srv.ai.calls == []


def test_stale_stage_proposal_is_refused(env):
    srv = env()
    hook(srv, msg(710, "Поставили образец защиты радиатора на Jolion"))
    p = find_project(srv, "HAVAL", "Защита радиатора")
    submit(srv, [stage_proposal(p["id"], "prototype", "documentation", 710, "образец")])
    s = session(srv)
    item = _open(srv, s)[0]
    r = s.post(srv.url + f"/api/ko/proposals/{item['id']}/apply", json={"version": p["version"]}, timeout=10)
    assert r.status_code == 409 and "устарело" in r.json()["error"]


def test_apply_assignee_deadline_comment_task_and_photo(env):
    srv = env()
    hook(srv, msg(800, "Ответственный по буксировочному на Jolion теперь Сидоров, срок до 15 декабря"))
    hook(srv, msg(801, "Петров, подготовь КД по буксировочному к пятнице"))
    hook(srv, msg(802, caption="буксировочное на авто", photo="p1"))
    hook(srv, msg(803, "Не совпали отверстия на кронштейне"))
    tow = find_project(srv, "HAVAL", "Буксировочное устройство")
    photo_id = messages(srv)["messages"][2]["photos"][0]["file_id"]
    ref = {"project_id": tow["id"]}
    base = {"project_ref": ref, "rationale": "в чате", "confidence": "high"}
    r = submit(srv, [
        dict(base, kind="assignee_change", change={"to": "Сидоров"}, summary="Новый ответственный", evidence=[ev(800, "теперь Сидоров")]),
        dict(base, kind="deadline_change", change={"to": "2026-12-15"}, summary="Новый срок", evidence=[ev(800, "срок до 15 декабря")]),
        dict(base, kind="new_task", change={"title": "Подготовить КД по буксировочному", "responsible": "Петров", "due": "2026-11-27"},
             summary="Поручение", evidence=[ev(801, "Петров, подготовь КД")]),
        dict(base, kind="photo_link", summary="Фото буксировочного", evidence=[ev(802, "буксировочное на авто")],
             photos=[{"file_id": photo_id, "observation": "фаркоп на автомобиле"}]),
        dict(base, kind="problem", summary="Не совпали отверстия", evidence=[ev(803, "не совпали отверстия")]),
    ])
    assert r.status_code == 201 and r.json()["counts"] == {"created": 5}, r.text
    s = session(srv)
    before_tasks = len(s.get(srv.url + "/api/tasks", timeout=10).json())
    for item in _open(srv, s):
        version = project(srv, s, tow["id"])["version"]
        res = s.post(srv.url + f"/api/ko/proposals/{item['id']}/apply", json={"version": version}, timeout=10)
        assert res.status_code == 200, (item["payload"]["kind"], res.text)
    after = project(srv, s, tow["id"])
    assert after["assigned_engineer"] == "Сидоров" and after["planned_deadline"] == "2026-12-15"
    assert after["field_sources"]["assigned_engineer"] == "manual", "applied as the person's own manual edit"
    assert any(c["text"].startswith("Проблема: Не совпали") for c in after["comments"])
    imgs = [i for i in after["images"] if i.get("telegram_message_id") == "802"]
    assert len(imgs) == 1
    tasks = s.get(srv.url + "/api/tasks", timeout=10).json()
    assert len(tasks) == before_tasks + 1 and tasks[-1]["title"] == "Подготовить КД по буксировочному"
    # a second photo proposal for the same Telegram message does not attach the photo twice
    again = submit(srv, [dict(base, kind="photo_link", summary="То же фото ещё раз", evidence=[ev(802, "буксировочное")],
                              photos=[{"file_id": photo_id}])])
    assert again.json()["counts"] == {"already_resolved": 1}
    assert len([i for i in project(srv, s, tow["id"])["images"] if i.get("telegram_message_id") == "802"]) == 1
    assert no_outbound_to_ko(srv) == []


def test_reject_is_final_and_unresolved_project_cannot_be_applied(env):
    srv = env()
    hook(srv, msg(900, "По Jolion всё сделали"))
    p = find_project(srv, "HAVAL", "Защита радиатора")
    submit(srv, [stage_proposal(p["id"], "measurement", p["status"], 900, "по jolion всё сделали")])
    # HAVAL Jolion matches two projects -> the reference stays unresolved
    amb = {"kind": "work_done", "project_ref": {"manufacturer": "HAVAL", "vehicle_model": "Jolion"}, "summary": "Сделали",
           "rationale": "общая фраза", "confidence": "medium", "evidence": [ev(900, "всё сделали")]}
    r = submit(srv, [amb])
    assert r.json()["results"][0]["resolved_project_id"] is None
    s = session(srv)
    items = {i["payload"]["kind"]: i for i in _open(srv, s)}
    res = s.post(srv.url + f"/api/ko/proposals/{items['work_done']['id']}/apply", json={}, timeout=10)
    assert res.status_code == 409
    rej = s.post(srv.url + f"/api/ko/proposals/{items['stage_change']['id']}/reject", json={"reason": "не так"}, timeout=10)
    assert rej.status_code == 200
    again = submit(srv, [stage_proposal(p["id"], "measurement", p["status"], 900, "по jolion всё сделали")])
    assert again.json()["counts"] == {"already_resolved": 1}, "a rejected proposal is not reopened"
    assert s.post(srv.url + f"/api/ko/proposals/{items['stage_change']['id']}/apply", json={"version": p["version"]}, timeout=10).status_code == 409
    assert project(srv, s, p["id"])["status"] == p["status"]


# ------------------------------------------------------------------ message types (archive v2)

def _media_msg(message_id, **fields):
    u = msg(message_id)
    u["message"].update(fields)
    return u


TYPE_CASES = [
    # (label, extra message fields, expected content_type, expected media kind or None, downloaded?)
    ("text", {"text": "Замер по Jolion сделали"}, "text", None, False),
    ("photo", {"photo": [{"file_id": "p1-s", "file_unique_id": "u-p1-s", "width": 90, "height": 90, "file_size": 10},
                         {"file_id": "p1", "file_unique_id": "u-p1", "width": 800, "height": 600, "file_size": 68}],
               "caption": "вид снизу"}, "photo", "photo", True),
    ("video", {"video": {"file_id": "v1", "file_unique_id": "u-v1", "duration": 12, "width": 1280, "height": 720,
                         "mime_type": "video/mp4", "file_size": 5000000, "file_name": "stand.mp4"}, "caption": "испытание"},
     "video", "video", False),
    ("video_note", {"video_note": {"file_id": "vn1", "file_unique_id": "u-vn1", "length": 240, "duration": 9, "file_size": 900000}},
     "video", "video_note", False),
    ("document", {"document": {"file_id": "d1", "file_unique_id": "u-d1", "file_name": "КД_333.0544.1.pdf",
                               "mime_type": "application/pdf", "file_size": 250000}, "caption": "КД"}, "document", "document", False),
    ("voice", {"voice": {"file_id": "vo1", "file_unique_id": "u-vo1", "duration": 31, "mime_type": "audio/ogg", "file_size": 60000}},
     "voice", "voice", False),
    ("audio", {"audio": {"file_id": "a1", "file_unique_id": "u-a1", "duration": 120, "mime_type": "audio/mpeg",
                         "file_size": 2000000, "file_name": "запись.mp3", "performer": "x", "title": "y"}}, "audio", "audio", False),
    ("sticker", {"sticker": {"file_id": "s1", "file_unique_id": "u-s1", "width": 512, "height": 512, "is_animated": False,
                             "is_video": False, "type": "regular", "emoji": "👍", "set_name": "pack", "file_size": 20000}},
     "sticker", "sticker", False),
    ("animation", {"animation": {"file_id": "g1", "file_unique_id": "u-g1", "duration": 3, "width": 320, "height": 240,
                                 "mime_type": "video/mp4", "file_size": 300000, "file_name": "a.gif.mp4"},
                   "document": {"file_id": "g1", "file_unique_id": "u-g1", "mime_type": "video/mp4", "file_name": "a.gif.mp4"}},
     "animation", "animation", False),
    ("service", {"new_chat_members": [{"id": 4242, "is_bot": True, "first_name": "bot"}]}, "service", None, False),
    ("other", {"poll": {"id": "1", "question": "?", "options": []}}, "other", None, False),
]


@pytest.mark.parametrize("label,fields,ctype,media_kind,downloaded", TYPE_CASES, ids=[c[0] for c in TYPE_CASES])
def test_each_message_type_is_archived_with_metadata(env, label, fields, ctype, media_kind, downloaded):
    srv = env()
    upd = _media_msg(1000, **fields)
    hook(srv, upd)
    hook(srv, upd)                                         # redelivery of the same update
    hook(srv, dict(upd, update_id=upd["update_id"] + 1))   # same message, new update_id
    page = messages(srv)
    assert len(page["messages"]) == 1, "no duplicates"
    m = page["messages"][0]
    assert m["content_type"] == ctype
    assert m["message_id"] == 1000 and m["date"] and m["from"]["id"] == "555"
    assert m["caption"] == fields.get("caption")
    if media_kind:
        x = m["media"][0]
        src = fields["photo"][-1] if media_kind == "photo" else fields[media_kind]
        assert x["kind"] == media_kind and x["file_unique_id"] == src["file_unique_id"] and x["has_file_id"] is True
        assert "file_id" not in x, "the Telegram file_id is never exposed by the API"
        for k in ("file_size", "file_name", "duration", "mime_type"):
            if k in src:
                assert x[k] == src[k], k
        assert x["download"] == ("stored" if downloaded else "metadata_only")
    else:
        assert m["media"] == []
    getfile = [c for c in srv.tg.calls if c[0] == "getFile"]
    assert len(getfile) == (1 if downloaded else 0), "only images are downloaded"
    if label == "service":
        assert m["service"] == {"event": "new_chat_members", "details": {"members": [{"id": "4242", "is_bot": True}]}}
    if label == "other":
        assert m["other_type"] == "poll"
    if label == "sticker":
        assert m["media"][0]["emoji"] == "👍" and m["media"][0]["mime_type"] == "image/webp"
    assert srv.ai.calls == [] and no_outbound_to_ko(srv) == []
    assert [c for c in srv.tg.calls if c[0] != "getFile"] == [], "no Telegram method other than getFile is ever called"


def test_media_files_are_not_downloaded_and_photos_still_are(env):
    srv = env()
    hook(srv, _media_msg(1100, **TYPE_CASES[2][1]))   # video
    hook(srv, _media_msg(1101, **TYPE_CASES[5][1]))   # voice
    hook(srv, _media_msg(1102, **TYPE_CASES[4][1]))   # pdf
    hook(srv, _media_msg(1103, **TYPE_CASES[1][1]))   # photo
    files = sorted(p.suffix for p in (srv.data_dir / "ko-files").rglob("*") if p.is_file())
    assert files == [".jpg"], files
    st = session(srv).get(srv.url + "/api/telegram/status", timeout=10).json()["engineering"]["ko_archive"]
    assert st["by_type"] == {"video": 1, "voice": 1, "document": 1, "photo": 1} and st["media_metadata_only"] == 3


def test_reply_context_quote_media_group_and_service_details(env):
    srv = env()
    # reply to a message the bot never saw (sent before it joined): its context is kept
    parent = {"message_id": 5, "date": 1795000000, "from": {"id": 9, "first_name": "Старший"},
              "text": "Нужно переделать крепление защиты на Tiggo 4", "chat": {"id": int(ENG_CHAT)}}
    hook(srv, _media_msg(1200, text="Сделаю к пятнице", reply_to_message=parent,
                         quote={"text": "переделать крепление", "position": 6}))
    m = messages(srv)["messages"][0]
    assert m["reply_to_message_id"] == 5 and m["thread_root_id"] == 5
    rc = m["reply_context"]
    assert rc["message_id"] == 5 and rc["kind"] == "text" and rc["text_excerpt"].startswith("Нужно переделать")
    assert m["quote"] == "переделать крепление"
    # reply to an archived message: no duplicated context, chain root resolved from the archive
    hook(srv, _media_msg(1201, text="ок", reply_to_message={"message_id": 1200, "chat": {"id": int(ENG_CHAT)}}))
    m2 = messages(srv, after=1)["messages"][0]
    assert m2["reply_context"] is None and m2["thread_root_id"] == 5
    # album: two photos with one media_group_id
    hook(srv, _media_msg(1202, media_group_id="alb1", caption="альбом", **{"photo": TYPE_CASES[1][1]["photo"]}))
    hook(srv, _media_msg(1203, media_group_id="alb1", photo=[{"file_id": "p2", "file_unique_id": "u-p2", "width": 10, "height": 10, "file_size": 72}]))
    album = [x for x in messages(srv)["messages"] if x["media_group_id"] == "alb1"]
    assert [x["message_id"] for x in album] == [1202, 1203]
    # service events carry their details; migration exposes the new chat id
    hook(srv, _media_msg(1204, migrate_to_chat_id=-1009999999999))
    hook(srv, _media_msg(1205, pinned_message={"message_id": 1200, "date": 1795000000, "chat": {"id": int(ENG_CHAT)}}))
    hook(srv, _media_msg(1206, forward_origin={"type": "user", "date": 1794000000, "sender_user": {"id": 1}}, text="переслано"))
    by = {x["message_id"]: x for x in messages(srv)["messages"]}
    assert by[1204]["service"] == {"event": "migrate_to_chat_id", "details": {"chat_id": "-1009999999999"}}
    assert by[1205]["service"] == {"event": "pinned_message", "details": {"message_id": 1200}}
    assert by[1206]["forwarded"] is True and by[1206]["forward"]["type"] == "user" and "sender_user" not in by[1206]["forward"]
    assert no_outbound_to_ko(srv) == [] and srv.ai.calls == []


def test_records_archived_before_the_upgrade_are_served_unchanged(env, tmp_path):
    """A message archived by the previous version (content_type other, no media/service fields)
    is returned as-is: nothing is rewritten, new fields are just empty."""
    srv = env()
    hook(srv, msg(1300, "до обновления"))
    srv.stop()
    data = json.loads(srv.data_file.read_text())
    rec = data["ko_messages"][0]
    for k in ("media", "service", "other_type", "forward", "reply_context", "quote", "external_reply"):
        rec.pop(k, None)
    rec["content_type"] = "other"
    srv.data_file.write_text(json.dumps(data))
    srv.start()
    m = messages(srv)["messages"][0]
    assert m["content_type"] == "other" and m["media"] == [] and m["service"] is None and m["text"] == "до обновления"
    raw = json.loads(srv.data_file.read_text())["ko_messages"][0]
    assert "media" not in raw, "reading never rewrites an old record"


# ------------------------------------------------------------------ processing cursor (daily run)

def test_cursor_is_server_side_monotonic_and_scoped(env):
    srv = env()
    assert ko(srv, "/cursor").json() == {"cursor": 0, "last_seq": 0, "updated_at": None, "run_id": None}
    hook(srv, msg(1500, "первое"))
    hook(srv, msg(1501, "второе"))
    assert ko(srv, "/cursor", method="PUT", json={"cursor": 3}).status_code == 409, "never beyond the archive"
    assert ko(srv, "/cursor", method="PUT", json={"cursor": -1}).status_code == 400
    assert ko(srv, "/cursor", method="PUT", json={"cursor": "x"}).status_code == 400
    r = ko(srv, "/cursor", method="PUT", json={"cursor": 2, "run_id": "daily-2026-11-23"})
    assert r.status_code == 200 and r.json() == {"cursor": 2, "previous": 0, "last_seq": 2}
    assert ko(srv, "/cursor", method="PUT", json={"cursor": 1}).status_code == 409, "never backwards"
    assert ko(srv, "/cursor", method="PUT", json={"cursor": 2}).status_code == 200, "idempotent re-save"
    c = ko(srv, "/cursor").json()
    assert c["cursor"] == 2 and c["run_id"] is None and c["updated_at"]
    assert messages(srv, after=c["cursor"])["messages"] == [], "the next run starts after the cursor"
    assert ko(srv, "/cursor", key=None).status_code == 401
    assert ko(srv, "/cursor", key=AGENT_KEY, method="PUT", json={"cursor": 2}).status_code == 401
    assert requests.get(srv.url + "/api/ko/proposals", timeout=10).status_code == 401
