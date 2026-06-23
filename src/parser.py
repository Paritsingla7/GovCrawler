import re

from bs4 import BeautifulSoup

# Obfuscation normalization — applied to full page text before email regex.
# Order matters: bracketed forms before bare words to avoid partial matches.
_OBFUSCATION_PATTERNS = [
    (re.compile(r'\s*\[at]\s*', re.IGNORECASE), '@'),
    (re.compile(r'\s*\(at\)\s*', re.IGNORECASE), '@'),
    (re.compile(r'\s+at\s+', re.IGNORECASE), '@'),
    (re.compile(r'\s*\[dot]\s*', re.IGNORECASE), '.'),
    (re.compile(r'\s*\(dot\)\s*', re.IGNORECASE), '.'),
    (re.compile(r'\s+dot\s+', re.IGNORECASE), '.'),
]

# Standard email regex — runs after obfuscation is normalized away.
# Greedy domain group captures full multi-part TLDs: nashik.gov.in, ias.nic.in
_EMAIL_REGEX = re.compile(
    r'[a-zA-Z0-9._%+\-]+'
    r'@'
    r'(?:[a-zA-Z0-9\-]+\.)+'
    r'[a-zA-Z]{2,6}',
    re.IGNORECASE
)

# Only keep addresses that end with a known Indian government suffix
_VALID_SUFFIXES = ('.gov.in', '.nic.in', '.res.in', '.ac.in')

# File extensions to skip — linked from gov pages but contain no parseable emails
_SKIP_EXTENSIONS = {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}


def parse_page_for_leads(html_content: str) -> list[dict]:
    """
    Two-step email extraction:
      1. Normalize obfuscation patterns across the full page text
      2. Run a standard email regex on the normalized text

    Handles: standard emails, [at]/(at)/bare-at obfuscation,
             [dot]/(dot)/bare-dot obfuscation, HTML entity encoding.
    Does not handle: image-based emails (needs OCR), PDF-embedded emails.
    """
    leads = []
    try:
        soup = BeautifulSoup(html_content, 'lxml')

        for tag in soup(['script', 'style', 'noscript']):
            tag.decompose()

        # BeautifulSoup decodes HTML entities automatically (&#101; → e)
        raw_text = soup.get_text(separator=' ')

        # Step 1: normalize obfuscation
        text = raw_text
        for pattern, replacement in _OBFUSCATION_PATTERNS:
            text = pattern.sub(replacement, text)

        # Step 2: extract and filter emails
        seen = set()
        for match in _EMAIL_REGEX.finditer(text):
            email = match.group(0).lower().strip('.')

            if not any(email.endswith(s) for s in _VALID_SUFFIXES):
                continue

            if email in seen:
                continue
            seen.add(email)

            start, end = match.span()
            snippet_start = max(0, start - 90)
            snippet_end = min(len(text), end + 90)
            context_snippet = ' '.join(text[snippet_start:snippet_end].split()).strip()

            leads.append({
                "email": email,
                "context_snippet": context_snippet,
            })

    except Exception:
        pass

    return leads
