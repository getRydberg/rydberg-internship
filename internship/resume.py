"""PDF text extraction and explainable resume vocabulary; no external AI service."""
import io
import re

from pypdf import PdfReader

SKILLS = {
    'python': ['python'], 'rust':['rust'], 'c++':['c++'], 'java':['java'],
    'javascript':['javascript','typescript'], 'sql':['sql','postgresql','mysql'],
    'react':['react'], 'linux':['linux'], 'security':['security','cybersecurity'],
    'firmware':['firmware'], 'embedded':['embedded','microcontroller','arduino'],
    'hardware':['hardware','fpga','verilog','vhdl'], 'machine learning':['machine learning','pytorch','tensorflow'],
    'data science':['data science','pandas','numpy'], 'software':['software','programming','developer'],
    'backend':['backend','back-end','django','flask','fastapi'], 'frontend':['frontend','front-end','react'],
    'cloud':['aws','azure','gcp','cloud'], 'devops':['devops','kubernetes','docker'],
    'product':['product management'], 'quantitative':['quantitative','quant','financial modeling'],
    'electrical':['electrical','circuits'], 'robotics':['robotics','ros'],
    'mobile':['android','ios','swift','kotlin'], 'go':['golang'], 'database':['database','databases'],
}


def contains(text, phrase):
    return bool(re.search(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', text, re.I))


def skill_terms(text):
    terms = {skill: 2 for skill, aliases in SKILLS.items() if any(contains(text, alias) for alias in aliases)}
    if set(terms) & {'python','rust','c++','java','javascript','backend','frontend','mobile'}:
        terms.setdefault('software', 2)
    if set(terms) & {'machine learning','data science'}:
        terms.setdefault('AI', 2)
    return terms


def extract_pdf(upload):
    if not upload or not upload.filename or not upload.filename.lower().endswith('.pdf'):
        raise ValueError('Choose a PDF resume.')
    data = upload.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        raise ValueError('Resume must be 5 MiB or smaller.')
    if not data.startswith(b'%PDF-'):
        raise ValueError('This file is not a PDF.')
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError('Upload an unencrypted PDF.')
        if not 1 <= len(reader.pages) <= 20:
            raise ValueError('Resume must contain between 1 and 20 pages.')
        parts, size = [], 0
        for page in reader.pages:
            # Bound decompressed page content before extraction.
            content = page.get_contents()
            if content and len(content.get_data()) > 10 * 1024 * 1024:
                raise ValueError('PDF page is too complex; export a simpler PDF.')
            part = page.extract_text() or ''
            size += len(part)
            if size > 100000:
                raise ValueError('Resume text is too long.')
            parts.append(part)
        text = '\n'.join(parts).strip().replace('\x00', '')
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('Could not read this PDF. Export it again and retry.') from exc
    if len(text) < 30:
        raise ValueError('No readable resume text found. Export a text PDF; scanned images need OCR first.')
    return text


def contact_hints(text):
    hints = {}
    email = re.search(r'[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}', text)
    if email:
        hints['email'] = email[0]
    for key, domain in [('linkedin','linkedin.com'), ('portfolio','github.com')]:
        link = re.search(r'(?:https?://)?(?:www\.)?' + re.escape(domain) + r'/[A-Za-z0-9_./\-]+', text)
        if link:
            hints[key] = link[0] if link[0].startswith('http') else 'https://' + link[0]
    return hints
