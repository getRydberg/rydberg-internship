"""Parse markdown-wrapped HTML tables without interpreting source age as a date."""
import hashlib
import json
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from .metadata import parse_terms

FLAGS = {"🔥": "faang_plus", "🛂": "no_sponsorship",
         "🇺🇸": "citizenship_required", "🎓": "advanced_degree"}
SECTIONS = ["Software Engineering", "Product Management", "Data Science/AI/ML",
            "Quantitative Finance", "Hardware Engineering"]


def clean(text):
    return " ".join(text.split())


def role_id(company, role, apply_url):
    parts = [clean(company).casefold(), clean(role).casefold(), apply_url.strip()]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def section_name(header):
    for name in SECTIONS:
        if name.casefold() in header.casefold():
            return name
    if "data science" in header.casefold():
        return SECTIONS[2]
    return None


def parse_readme(markdown, default_term=None):
    roles = []
    # Headers delimit company continuation scope. Parse only recognized sections.
    chunks = re.split(r"(?m)^\s{0,3}##\s+([^\n]+)\n", markdown)
    for header, body in zip(chunks[1::2], chunks[2::2]):
        section = section_name(header)
        if not section:
            continue
        soup = BeautifulSoup(re.sub(r"<br\s*/?>", "\n", body, flags=re.I), "html.parser")
        for table in soup.find_all("table"):
            company, company_flags = None, {}
            for row in table.find_all("tr"):
                cells = row.find_all("td", recursive=False)
                if not cells:
                    continue
                if len(cells) not in (5, 6):
                    raise ValueError("Expected five or six cells in a role row")
                company_cell, title_cell, location_cell = cells[:3]
                app_cell, age_cell = cells[-2:]
                term_text = cells[3].get_text(' ', strip=True) if len(cells) == 6 else ''
                text = clean(company_cell.get_text(" ", strip=True))
                if "↳" in text:
                    if company is None:
                        raise ValueError("Company continuation without preceding company")
                else:
                    company_flags = {flag: emoji in text for emoji, flag in FLAGS.items()}
                    for emoji in FLAGS:
                        text = text.replace(emoji, "")
                    company = clean(text)
                title = clean(title_cell.get_text(" ", strip=True))
                flags = dict(company_flags)
                # Upstream also places eligibility flags in the role cell.
                for emoji, flag in FLAGS.items():
                    flags[flag] = flags[flag] or emoji in title
                    title = title.replace(emoji, "")
                title = clean(title)
                for summary in location_cell.find_all("summary"):
                    if re.fullmatch(r"\d+\s+locations?", summary.get_text(strip=True), re.I):
                        summary.decompose()
                for br in location_cell.find_all("br"):
                    br.replace_with("\n")
                location_text = re.sub(r"^\s*\d+\s+locations?\s*", "", location_cell.get_text(), flags=re.I)
                locations = [clean(part) for part in location_text.splitlines() if clean(part)]
                link = app_cell.find("a", href=True)
                apply_url = link["href"].strip() if link else ""
                if apply_url and urlsplit(apply_url).scheme not in ("http", "https"):
                    raise ValueError("Unsafe application URL")
                closed = "🔒" in app_cell.get_text()
                if not company or not title or (not apply_url and not closed):
                    raise ValueError("Incomplete role row")
                roles.append(dict(id=role_id(company, title, apply_url), company=company,
                                  role=title, section=section, locations=locations, flags=flags,
                                  apply_url=apply_url, closed=closed,
                                  source_age=clean(age_cell.get_text(" ", strip=True)),
                                  terms=parse_terms(term_text or title, default_term)))
    if not roles:
        raise ValueError("No roles parsed; refusing to mark existing roles gone")
    return roles
