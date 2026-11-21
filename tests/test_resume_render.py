"""Resume text -> blocks -> docx: project-style resumes (Project N / Client: / Role: ...) and the built-in sample."""
import json
from collections import Counter
from pathlib import Path

import resume_template as rt

FIX = Path(__file__).parent / "fixtures"


def kinds(text):
    return Counter(b[0] for b in rt.parse_blocks(text))


def test_sample_resume_parse_unchanged():
    blocks = [list(b) for b in rt.parse_blocks(rt.SAMPLE_RESUME)]
    golden = json.loads((FIX / "sample_resume_blocks.json").read_text())
    trim = lambda b: b[:-1] if b and b[0] == "job" and len(b) == 5 and b[4] == "" else b
    assert [trim(b) for b in blocks] == [trim(b) for b in golden]


def test_project_style_resume_blocks():
    text = (FIX / "resume_project_style.txt").read_text()
    blocks = rt.parse_blocks(text)
    k = Counter(b[0] for b in blocks)
    assert k["project"] == 4 and k["section"] == 4 and k["job"] == 2
    assert k["field"] == 20                                   # Client/Role/Duration/Description/Outcome/Key Accomplishments
    assert ("label", "Some of Jordan's notable engagements include:") in blocks
    assert ("section", "Relevant Consulting and Advisory Engagements") in blocks
    assert ("field", "Key Accomplishments", "") in blocks
    jobs = [b for b in blocks if b[0] == "job"]
    assert jobs[0][4] == "Washington, DC"                     # location line is part of the header, not a bullet
    assert not any(b[0] == "bullet" and b[1] == "Washington, DC" for b in blocks)


def test_colon_lead_in_is_not_a_bullet():
    assert rt.parse_blocks("Name\n\nSome intro:\nplain text here")[1][0] == "label"
    assert rt.parse_blocks("Name\n\nSKILLS\nPython: expert")[2] == ("skill", "Python", "expert")


def test_docx_has_spacing_between_projects_and_bold_labels(tmp_path, monkeypatch):
    import storage
    monkeypatch.setenv("JSHQ_DATA_DIR", str(tmp_path))
    rt.install_default_template("t1")
    out = rt.render((FIX / "resume_project_style.txt").read_text(), "t1", tmp_path / "r.docx")
    from docx import Document
    doc = Document(out)
    texts = [p.text for p in doc.paragraphs]
    i = texts.index("Project 2: Control Framework Assessments")
    assert texts[i - 1] == ""                                 # blank spacer before every project but the first
    first = next(p for p in doc.paragraphs if p.text.startswith("Client: International"))
    assert first.runs[0].bold and first.runs[0].text == "Client: "
    assert not first._p.pPr.numPr if first._p.pPr is not None else True   # not a bullet
