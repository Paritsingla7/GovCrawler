import re
from bs4 import BeautifulSoup

def parse_page_for_leads(html_content: str) -> list[dict]:
    """
    Parses HTML content to extract emails and their context.

    - Strips script/style tags for cleaner text.
    - Uses a robust regex to find standard and obfuscated emails.
    - Extracts a clean context snippet around the original match.

    Args:
        html_content: The raw HTML of the page.

    Returns:
        A list of dictionaries, where each dictionary represents a found lead.
    """
    leads = []
    try:
        soup = BeautifulSoup(html_content, 'lxml')

        # Remove script and style tags to avoid parsing irrelevant text
        for script_or_style in soup(['script', 'style']):
            script_or_style.decompose()

        # Get text, but preserve some line breaks for context
        text = soup.get_text(separator=' ')

        # This regex handles: user@domain.com, user [at] domain [dot] com, user at domain dot com, etc.
        email_regex = re.compile(
            r'([a-zA-Z0-9._%+-]+)\s*(?:@|\[at\]|\(at\)| at )\s*([a-zA-Z0-9.-]+)\s*(?:\.|\[dot\]|\(dot\)| dot )\s*([a-zA-Z]{2,6})',
            re.IGNORECASE
        )

        for match in email_regex.finditer(text):
            # Normalize the email into the standard user@domain.com format
            normalized_email = f"{match.group(1)}@{match.group(2)}.{match.group(3)}"

            # Extract a context snippet around the original matched text
            start, end = match.span()
            snippet_start = max(0, start - 90)
            snippet_end = min(len(text), end + 90)
            
            raw_snippet = text[snippet_start:snippet_end]
            
            # Clean up newlines and extra whitespace for a neat, single-line CSV entry
            context_snippet = ' '.join(raw_snippet.split()).strip()

            leads.append({
                "email": normalized_email.lower(), # Standardize email to lowercase
                "context_snippet": context_snippet
            })
    except Exception:
        # If parsing fails for any reason, return an empty list.
        pass
        
    return leads
