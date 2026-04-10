"""
Pluggable intake module for job postings.

Each intake source implements the IntakeHandler protocol:
  - parse(raw_input) -> dict with keys: raw_jd, title, company, source, url, pay_range

To add a new intake source:
1. Create a class implementing parse()
2. Register it in INTAKE_HANDLERS
"""

from abc import ABC, abstractmethod
from typing import Optional
import httpx
from bs4 import BeautifulSoup


class IntakeHandler(ABC):
    """Base class for all intake handlers."""

    @abstractmethod
    def parse(self, raw_input: str) -> dict:
        """Parse raw input into structured job data.
        Returns dict with: raw_jd, title, company, source, url, pay_range
        """
        pass


class ManualPasteHandler(IntakeHandler):
    """User pastes raw JD text directly."""

    def parse(self, raw_input: str) -> dict:
        return {
            "raw_jd": raw_input.strip(),
            "title": "",
            "company": "",
            "source": "manual_paste",
            "url": "",
            "pay_range": "",
        }


class URLScrapeHandler(IntakeHandler):
    """Scrape a job posting from a URL."""

    def parse(self, raw_input: str) -> dict:
        url = raw_input.strip()
        try:
            resp = httpx.get(url, follow_redirects=True, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")

            # Remove script/style
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()

            text = soup.get_text(separator="\n", strip=True)
            # Truncate to reasonable length
            text = text[:8000]

            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else ""

            return {
                "raw_jd": text,
                "title": title,
                "company": "",
                "source": "url_scrape",
                "url": url,
                "pay_range": "",
            }
        except Exception as e:
            return {
                "raw_jd": f"(Failed to scrape URL: {e})",
                "title": "",
                "company": "",
                "source": "url_scrape",
                "url": url,
                "pay_range": "",
            }


class EmailForwardHandler(IntakeHandler):
    """Parse a forwarded email body containing a job posting.
    
    Future: integrate with Gmail API to poll a label.
    For now, accepts the email body text.
    """

    def parse(self, raw_input: str) -> dict:
        # Strip common email forwarding artifacts
        lines = raw_input.strip().split("\n")
        cleaned = []
        skip_headers = True
        for line in lines:
            # Skip forwarded email headers
            if skip_headers and any(line.startswith(h) for h in 
                ["From:", "To:", "Subject:", "Date:", "Sent:", "------"]):
                continue
            skip_headers = False
            cleaned.append(line)

        return {
            "raw_jd": "\n".join(cleaned).strip(),
            "title": "",
            "company": "",
            "source": "email_forward",
            "url": "",
            "pay_range": "",
        }


# Registry of available handlers
INTAKE_HANDLERS: dict[str, IntakeHandler] = {
    "manual_paste": ManualPasteHandler(),
    "url_scrape": URLScrapeHandler(),
    "email_forward": EmailForwardHandler(),
}


def process_intake(source: str, raw_input: str) -> dict:
    """Process input through the appropriate handler."""
    handler = INTAKE_HANDLERS.get(source, INTAKE_HANDLERS["manual_paste"])
    return handler.parse(raw_input)
