import io
import os
import sys
import tempfile
from pathlib import Path

import pytest

_DATA = tempfile.mkdtemp(prefix="jshq-test-")
os.environ["JSHQ_DATA_DIR"] = _DATA
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import storage  # noqa: E402
import writing_samples as ws  # noqa: E402

UID = "user-a"
SAMPLE = "Dear team, I like short sentences. They land. Plain words beat fancy ones."


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)


def test_add_list_get_delete():
    meta = ws.add_sample(UID, "Blog post", SAMPLE)
    assert [m["id"] for m in ws.list_samples(UID)] == [meta["id"]]
    assert ws.get_text(UID, meta["id"]) == SAMPLE
    assert ws.delete_sample(UID, meta["id"])
    assert ws.list_samples(UID) == []
    assert not ws.delete_sample(UID, meta["id"])


def test_users_are_isolated():
    meta = ws.add_sample("user-a", "A", SAMPLE)
    assert ws.list_samples("user-b") == []
    assert ws.get_text("user-b", meta["id"]) is None
    assert not ws.delete_sample("user-b", meta["id"])


def test_path_traversal_ids_rejected():
    for bad in ("../../etc/passwd", "..", "", "ZZZZZZZZZZZZ", "a" * 40):
        assert ws.get_text(UID, bad) is None
        assert not ws.delete_sample(UID, bad)


def test_extract_txt_docx_pdf():
    assert "short sentences" in ws.extract_text(SAMPLE.encode(), "s.txt")
    from docx import Document
    d = Document(); d.add_paragraph("First para."); d.add_paragraph("Second para.")
    buf = io.BytesIO(); d.save(buf)
    assert ws.extract_text(buf.getvalue(), "s.docx") == "First para.\nSecond para."
    from pypdf import PdfWriter
    w = PdfWriter(); w.add_blank_page(width=200, height=200)
    pdf = io.BytesIO(); w.write(pdf)
    assert ws.extract_text(pdf.getvalue(), "s.pdf") == ""  # readable PDF, no text layer


def test_extract_rejects_bad_files():
    with pytest.raises(ValueError):
        ws.extract_text(b"x", "s.exe")
    with pytest.raises(ValueError):
        ws.extract_text(b"not a docx", "s.docx")
    with pytest.raises(ValueError):
        ws.extract_text(b"not a pdf", "s.pdf")


def test_empty_and_limits():
    with pytest.raises(ValueError):
        ws.add_sample(UID, "x", "   \n ")
    meta = ws.add_sample(UID, "big", "word " * 10_000)
    assert meta["truncated"] and meta["chars"] == ws.MAX_SAMPLE_CHARS
    for i in range(ws.MAX_SAMPLES - 1):
        ws.add_sample(UID, f"s{i}", SAMPLE)
    with pytest.raises(ValueError):
        ws.add_sample(UID, "one too many", SAMPLE)


def test_prompt_text_budget_and_toggle():
    assert ws.prompt_text(UID) == ""
    for i in range(4):
        ws.add_sample(UID, f"s{i}", f"S{i} " + "x" * 10_000)
    out = ws.prompt_text(UID)
    assert 0 < len(out) <= ws.PROMPT_BUDGET_CHARS + 4 * len("\n\n---\n\n")
    assert all(f"S{i} " in out for i in range(4))
    ws.set_enabled(UID, False)
    assert ws.prompt_text(UID) == ""
    assert ws.with_cover_letter_guidance("SYS", UID) == "SYS"
    ws.set_enabled(UID, True)
    assert "WRITING SAMPLES" in ws.with_cover_letter_guidance("SYS", UID)


def test_guidance_survives_braces_in_samples():
    ws.add_sample(UID, "braces", "I write {curly} and {0} things")
    assert "{curly}" in ws.with_resume_guidance("SYS", UID)


def _job():
    from models import Job
    return Job(id="j1", title="Analyst", company="Acme", raw_jd="Do analysis.")


def test_generation_prompts_include_samples(monkeypatch):
    import scoring
    ws.add_sample(UID, "mine", SAMPLE)
    storage.save_resume_text(UID, "base", "Resume body")
    seen = []
    monkeypatch.setattr(scoring.ai_router, "chat", lambda system, user_msg, *a, **k: seen.append(system) or "out")
    scoring.generate_cover_letters(_job(), UID)
    scoring.generate_tailored_resume(_job(), UID)
    assert len(seen) == 4 and all("Plain words beat fancy ones" in s for s in seen)
    seen.clear()
    ws.set_enabled(UID, False)
    scoring.generate_cover_letters(_job(), UID)
    assert seen and all("Plain words" not in s for s in seen)


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    import main
    from models import User
    main.app.dependency_overrides[main.get_current_user] = lambda: User(id=UID, email="t@example.com", name="T")
    monkeypatch.setattr(main, "verify_agent_api_key", lambda k: k == "good")
    monkeypatch.setattr(main, "get_admin_user_id", lambda: UID)
    yield TestClient(main.app)
    main.app.dependency_overrides.clear()


def test_api_roundtrip(client):
    r = client.post("/api/writing-samples", json={"name": "pasted", "content": SAMPLE})
    assert r.status_code == 200
    sid = r.json()["id"]
    r = client.post("/api/writing-samples/upload", files={"file": ("note.txt", SAMPLE.encode())})
    assert r.status_code == 200 and r.json()["name"] == "note"
    assert len(client.get("/api/writing-samples").json()["samples"]) == 2
    assert client.get(f"/api/writing-samples/{sid}").json()["content"] == SAMPLE
    assert client.post("/api/writing-samples/upload", files={"file": ("a.exe", b"x")}).status_code == 400
    assert client.post("/api/writing-samples", json={"content": " "}).status_code == 400
    assert client.put("/api/writing-samples/settings", json={"enabled": "yes"}).status_code == 400
    assert client.put("/api/writing-samples/settings", json={"enabled": False}).json()["enabled"] is False
    assert client.get("/api/writing-samples").json()["enabled"] is False
    assert client.delete(f"/api/writing-samples/{sid}").status_code == 200
    assert client.delete(f"/api/writing-samples/{sid}").status_code == 404
    assert client.get(f"/api/writing-samples/{sid}").status_code == 404


def test_agent_endpoint(client):
    ws.add_sample(UID, "mine", SAMPLE)
    assert client.get("/api/agent/writing-samples").status_code == 401
    assert client.get("/api/agent/writing-samples", headers={"X-API-Key": "bad"}).status_code == 401
    body = client.get("/api/agent/writing-samples", headers={"X-API-Key": "good"}).json()
    assert body["enabled"] and body["samples"][0]["text"] == SAMPLE and SAMPLE in body["prompt_text"]
    ws.set_enabled(UID, False)
    body = client.get("/api/agent/writing-samples", headers={"X-API-Key": "good"}).json()
    assert body == {"enabled": False, "samples": [], "prompt_text": ""}
