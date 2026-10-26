"""
User-uploaded resume templates.

A user uploads a .docx whose layout they like (e.g. a SheetsResume-style sample).
We learn one "prototype" paragraph per resume role (name, contact, section
heading, company line, title line, bullet, sub-bullet, spacer) from that file,
then build tailored resumes by cloning those paragraphs' formatting — fonts,
borders, numbering, spacing — and filling in the tailored text. The template's
own sample content is discarded.

Storage: data/users/<user_id>/resume_template.docx (+ resume_template.json).
"""

import copy
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from docx import Document
from docx.enum.text import WD_TAB_ALIGNMENT
from docx.oxml.ns import qn
from docx.shared import Twips
from docx.text.paragraph import Paragraph

import storage

logger = logging.getLogger(__name__)

MAX_TEMPLATE_BYTES = 5 * 1024 * 1024

DATE_PATTERN = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|Present|\d{4})"
KNOWN_SECTIONS = {
    "SUMMARY", "CORE CAPABILITIES", "PROFESSIONAL EXPERIENCE",
    "EARLIER EXPERIENCE", "TECHNICAL SKILLS", "CERTIFICATIONS",
    "EDUCATION", "CERTIFICATIONS & EDUCATION", "SELECTED INNOVATION",
    "SELECTED SYSTEMS & INNOVATION", "PROJECT HIGHLIGHTS", "KEY SKILLS",
}

SAMPLE_RESUME = """JANE SAMPLE
jane@example.com · (555) 555-0100 · Washington, DC · linkedin.com/in/janesample

SUMMARY
Data and analytics leader with 12 years of experience building BI teams.

PROFESSIONAL EXPERIENCE
Director of Analytics
Example Corp · Jan 2020 – Present
• Led a 15-person analytics organization supporting $2B in annual revenue.
  - Cut reporting turnaround from 5 days to 1 day.
• Built the enterprise data platform on Snowflake and dbt.

Senior Manager, Business Intelligence
Sample Industries · Mar 2015 – Dec 2019
• Delivered self-service dashboards adopted by 400+ users.

TECHNICAL SKILLS
Languages: SQL; Python; R
Platforms: Snowflake; Tableau; Power BI

EDUCATION
M.S., Information Systems · Example University · 2012
"""


# ── Storage ────────────────────────────────────────────────────────────────────

def template_path(user_id: str) -> Path:
    return storage.get_user_dir(user_id) / "resume_template.docx"


def _meta_path(user_id: str) -> Path:
    return storage.get_user_dir(user_id) / "resume_template.json"


def has_template(user_id: str) -> bool:
    return template_path(user_id).exists()


def template_info(user_id: str) -> dict:
    if not has_template(user_id):
        return {"loaded": False}
    meta = {}
    try:
        meta = json.loads(_meta_path(user_id).read_text())
    except Exception:
        pass
    return {"loaded": True, **meta}


def delete_template(user_id: str) -> None:
    template_path(user_id).unlink(missing_ok=True)
    _meta_path(user_id).unlink(missing_ok=True)


def save_template(user_id: str, data: bytes, filename: str) -> dict:
    """Validate and store an uploaded template. Raises ValueError with a user-facing message."""
    if not filename.lower().endswith(".docx"):
        raise ValueError("Only .docx files are supported")
    if len(data) > MAX_TEMPLATE_BYTES:
        raise ValueError("Template is too large (5 MB max)")
    if not data.startswith(b"PK"):
        raise ValueError("That file is not a valid .docx")

    storage.ensure_user_dirs(user_id)
    tmp = template_path(user_id).with_suffix(".upload")
    tmp.write_bytes(data)
    try:
        with zipfile.ZipFile(tmp) as z:
            names = z.namelist()
            if "word/document.xml" not in names:
                raise ValueError("That file is not a valid .docx")
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                raise ValueError("Macro-enabled documents are not allowed")
        protos = analyze(tmp)
    except zipfile.BadZipFile:
        tmp.unlink(missing_ok=True)
        raise ValueError("That file is not a valid .docx")
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(template_path(user_id))

    info = {
        "filename": Path(filename).name,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "roles": sorted(protos.keys()),
    }
    _meta_path(user_id).write_text(json.dumps(info))
    return {"loaded": True, **info}


# ── Template analysis ──────────────────────────────────────────────────────────

def _numbering(p):
    """(numId, ilvl) for a numbered/bulleted paragraph, else None."""
    ppr = p._p.pPr
    if ppr is None or ppr.numPr is None:
        return None
    ilvl = ppr.numPr.ilvl.val if ppr.numPr.ilvl is not None else 0
    return ppr.numPr.numId.val, ilvl


def _has_bottom_border(p) -> bool:
    ppr = p._p.pPr
    return ppr is not None and ppr.find(qn("w:pBdr")) is not None


def _has_tab(p) -> bool:
    return any(r._r.findall(qn("w:tab")) for r in p.runs)


def _first_bold(p) -> bool:
    runs = [r for r in p.runs if r.text.strip()]
    return bool(runs) and bool(runs[0].bold)


def analyze(path) -> dict:
    """Find one prototype paragraph per resume role. Raises ValueError if unusable."""
    doc = Document(str(path))
    paras = doc.paragraphs
    filled = [p for p in paras if p.text.strip()]
    if len(filled) < 4:
        raise ValueError("That template has too little content to learn a layout from")

    protos = {"name": filled[0], "contact": filled[1]}
    for p in paras:
        if not p.text.strip():
            if "spacer" not in protos and p._p.pPr is not None:
                protos["spacer"] = p
            continue
        if p is filled[0] or p is filled[1]:
            continue
        num = _numbering(p)
        if num:
            key = "sub" if num[1] >= 1 else "bullet"
        elif _has_bottom_border(p):
            key = "section"
        elif _has_tab(p) and _first_bold(p):
            key = "org"
        elif _has_tab(p):
            key = "title"
        else:
            key = None
        if key and key not in protos:
            protos[key] = p

    if "section" not in protos:
        raise ValueError(
            "Couldn't find a section heading in that template "
            "(expected a heading with an underline/border, e.g. WORK EXPERIENCE)"
        )
    if "bullet" not in protos:
        raise ValueError("Couldn't find a bulleted line in that template")
    return protos


def _text_width_twips(doc) -> int:
    sec = doc.sections[0]
    return int((sec.page_width - sec.left_margin - sec.right_margin) / 635)  # EMU → twips


def _rpr_of(p, plain: bool = False):
    """rPr of a prototype's first direct text run (preferring a non-bold run if plain)."""
    runs = [r for r in p.runs if r.text.strip()] or list(p.runs)
    if plain:
        non_bold = [r for r in runs if not r.bold]
        runs = non_bold or runs
    for r in runs:
        if r._r.rPr is not None:
            return copy.deepcopy(r._r.rPr)
    ppr = p._p.pPr
    if ppr is not None and ppr.find(qn("w:rPr")) is not None:
        return copy.deepcopy(ppr.find(qn("w:rPr")))
    return None


# ── Text → blocks ──────────────────────────────────────────────────────────────
# Mirrors the line heuristics in docx_builder.generate_resume_docx so the same
# LLM output is understood either way.

def parse_blocks(text: str) -> list[tuple]:
    lines = text.strip().split("\n")
    blocks: list[tuple] = []
    i = 0
    in_header = True
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
            continue

        is_section = (
            (line.isupper() and len(line) > 3 and line[0] not in "•-*")
            or line.upper().rstrip(":") in KNOWN_SECTIONS
        )

        if in_header and not blocks:
            blocks.append(("name", line))
            i += 1
            continue
        if in_header and len(blocks) == 1:
            in_header = False
            if "·" in line or "@" in line or "linkedin" in line.lower():
                blocks.append(("contact", line))
                i += 1
                continue

        in_header = False

        if is_section:
            blocks.append(("section", line.rstrip(":")))
            i += 1
            continue

        if line[0] in "•-*":
            indent = len(raw) - len(raw.lstrip())
            blocks.append(("sub" if indent >= 2 else "bullet", line.lstrip("•-* ").strip()))
            i += 1
            continue

        if re.match(r"^(BI |Senior |Operations |IT |Co-Founder|Product )", line) and "(" in line:
            m = re.match(r"^(.*?)\s*\((.+)\)\s*$", line)
            blocks.append(("subheader", m.group(1), m.group(2)) if m else ("subheader", line, ""))
            i += 1
            continue

        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
        looks_like_title = (
            len(line) < 80
            and next_line
            and ("–" in next_line or "—" in next_line or "·" in next_line or re.match(r"^[A-Z]", next_line))
        )
        if re.search(DATE_PATTERN, next_line) and looks_like_title:
            org, dates = next_line, ""
            if "\t" in next_line:
                parts = next_line.split("\t")
                org, dates = parts[0].strip(), parts[-1].strip() if len(parts) > 1 else ""
            else:
                m = re.search(r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|Present)[\w\s,–\-]*\d{4}.*$)", next_line)
                if m:
                    org, dates = next_line[:m.start()].rstrip(" ·–—,"), m.group(1).strip()
            blocks.append(("job", line, org, dates))
            i += 2
            continue

        if ":" in line and len(line.split(":")[0]) < 25:
            label, value = line.split(":", 1)
            blocks.append(("skill", label, value.strip()))
            i += 1
            continue

        blocks.append(("body", line))
        i += 1
    return blocks


# ── Rendering ──────────────────────────────────────────────────────────────────

class _Builder:
    def __init__(self, template: Path):
        self.doc = Document(str(template))
        self.protos = analyze(template)
        body = self.doc.element.body
        for child in list(body):
            if child.tag != qn("w:sectPr"):
                body.remove(child)
        self._sect = body.find(qn("w:sectPr"))
        self.width = _text_width_twips(self.doc)
        self._rpr = {k: _rpr_of(p, plain=k in ("bullet", "sub")) for k, p in self.protos.items()}

    def _fallback(self, role: str) -> str:
        chain = {"sub": "bullet", "org": "title", "title": "org", "contact": "name",
                 "spacer": None}
        while role not in self.protos:
            role = chain.get(role) or "bullet"
        return role

    def _new(self, role: str, right_tab: bool = False) -> Paragraph:
        role = self._fallback(role)
        proto = self.protos[role]
        el = copy.deepcopy(proto._p)
        for child in list(el):
            if child.tag != qn("w:pPr"):
                el.remove(child)
        self._sect.addprevious(el)
        p = Paragraph(el, proto._parent)
        if right_tab:
            p.paragraph_format.tab_stops.clear_all()
            p.paragraph_format.tab_stops.add_tab_stop(Twips(self.width), WD_TAB_ALIGNMENT.RIGHT)
        p._role = role
        return p

    def _run(self, p: Paragraph, text: str, bold=None, italic=None):
        run = p.add_run(text)
        rpr = self._rpr.get(p._role)
        if rpr is not None:
            run._r.insert(0, copy.deepcopy(rpr))
        if bold is not None:
            run.bold = bold
        if italic is not None:
            run.italic = italic
        return run

    def _fit_to_line(self, run, text: str):
        """Shrink a one-line run (contact info) so it doesn't wrap, down to a 9pt floor."""
        # Templates' sample contact lines sit near the edge, and the real font may be wider, so keep ~15% slack.
        sample = int(0.85 * (len(self.protos["contact"].text) or len(text)))
        if len(text) <= sample:
            return
        rpr = run._r.rPr
        sz = rpr.find(qn("w:sz")) if rpr is not None else None
        if sz is None:
            return
        half_pts = max(18, int(int(sz.get(qn("w:val"))) * sample / len(text)))
        sz.set(qn("w:val"), str(half_pts))
        cs = rpr.find(qn("w:szCs"))
        if cs is not None:
            cs.set(qn("w:val"), str(half_pts))

    def spacer(self):
        if "spacer" in self.protos:
            self._new("spacer")

    def add(self, block: tuple):
        kind = block[0]
        if kind in ("name", "contact", "section", "body"):
            text = block[1]
            if kind == "contact":
                text = re.sub(r"\s*·\s*", " | ", text)
            p = self._new({"body": "bullet"}.get(kind, kind))
            if kind == "body":  # un-numbered plain line: reuse bullet formatting without the bullet
                num = p._p.pPr.numPr
                if num is not None:
                    p._p.pPr.remove(num)
            run = self._run(p, text)
            if kind == "contact":
                self._fit_to_line(run, text)
        elif kind in ("bullet", "sub"):
            self._run(self._new(kind), block[1])
        elif kind == "skill":
            p = self._new("bullet")
            self._run(p, block[1] + ": ", bold=True)
            self._run(p, block[2], bold=False)
        elif kind == "job":
            _, title, org, dates = block
            p = self._new("org", right_tab=True)
            self._run(p, org, bold=True)
            if dates:
                self._run(p, "\t", bold=True)
                self._run(p, dates, bold=True)
            p = self._new("title")
            self._run(p, title, italic=True)
        elif kind == "subheader":
            _, title, dates = block
            p = self._new("title", right_tab=True)
            self._run(p, title, italic=True)
            if dates:
                self._run(p, "\t", italic=True)
                self._run(p, dates, italic=True)


def render(tailored_text: str, user_id: str, out_path: Path) -> str:
    """Build a resume from the user's template. Returns out_path."""
    b = _Builder(template_path(user_id))
    prev = None
    for block in parse_blocks(tailored_text):
        kind = block[0]
        if kind == "section" and prev is not None:
            b.spacer()
        elif kind == "job" and prev in ("bullet", "sub", "skill", "job", "body"):
            b.spacer()
        b.add(block)
        prev = kind
    b.doc.save(str(out_path))
    return str(out_path)


def render_sample(user_id: str, out_path: Path) -> str:
    return render(SAMPLE_RESUME, user_id, out_path)
