import markdown
from pymdownx import emoji

try:
    import nh3
except ImportError:  # pragma: no cover - sanitising is strongly recommended
    nh3 = None

MD_EXTENSIONS = [
    'tables', 'fenced_code', 'admonition', 'toc', 'footnotes', 'def_list', 'abbr',
    'pymdownx.tasklist', 'pymdownx.caret', 'pymdownx.mark', 'pymdownx.tilde',
    'pymdownx.smartsymbols', 'pymdownx.magiclink', 'pymdownx.saneheaders',
    'pymdownx.emoji', 'pymdownx.keys', 'pymdownx.superfences', 'pymdownx.highlight',
    'pymdownx.arithmatex',
]

MD_EXTENSION_CONFIGS = {
    'pymdownx.emoji': {
        'emoji_index': emoji.gemoji,
        'emoji_generator': emoji.to_alt,
    },
    'pymdownx.highlight': {
        # leave colouring to highlight.js on the client, as before
        'use_pygments': False,
    },
}

# ---------------------------------------------------------------- sanitising (untrusted writers)

_ALLOWED_TAGS = {
    'a', 'abbr', 'b', 'blockquote', 'br', 'code', 'dd', 'del', 'details', 'div', 'dl', 'dt',
    'em', 'figcaption', 'figure', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'input',
    'ins', 'kbd', 'li', 'mark', 'ol', 'p', 'pre', 's', 'small', 'span', 'strong', 'sub',
    'summary', 'sup', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'u', 'ul',
    'video', 'audio', 'source', 'section',
}
_ALLOWED_ATTRS = {
    '*': {'class', 'id', 'title', 'style'},
    'a': {'href'},
    'abbr': {'title'},
    'img': {'src', 'alt', 'width', 'height', 'loading'},
    'input': {'type', 'checked', 'disabled'},
    'video': {'src', 'controls', 'preload', 'poster', 'loop', 'muted', 'playsinline', 'width', 'height'},
    'audio': {'src', 'controls', 'preload', 'loop'},
    'source': {'src', 'type'},
    'td': {'align', 'colspan', 'rowspan'},
    'th': {'align', 'colspan', 'rowspan'},
    'details': {'open'},
    'ol': {'start'},
}


def sanitise_html(html: str) -> str:
    if nh3 is None:
        raise RuntimeError('nh3 is required to render untrusted markdown (pip install nh3)')
    return nh3.clean(
        html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS,
        url_schemes={'http', 'https', 'mailto'},
        filter_style_properties={'text-align', 'width', 'height'},
        link_rel='noopener noreferrer',
    )


def render_markdown(content: str, trusted: bool = True) -> str:
    """Render markdown to HTML. Untrusted authors get their HTML sanitised."""
    html = markdown.markdown(content or '', extensions=MD_EXTENSIONS,
                             extension_configs=MD_EXTENSION_CONFIGS)
    return html if trusted else sanitise_html(html)


class RenderedMarkdown:
    """Kept for backwards compatibility with the original API."""

    def __init__(self, path: str = None, content: str = None, trusted: bool = True):
        if path:
            with open(path, encoding='utf-8') as f:
                content = f.read()
        self.output = render_markdown(content or '', trusted)

    def __str__(self):
        return self.output
