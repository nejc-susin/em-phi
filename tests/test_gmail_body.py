import base64

from em_phi.providers.gmail import _extract_body


def _data(text: str, charset: str = "utf-8") -> dict:
    return {"data": base64.urlsafe_b64encode(text.encode(charset)).decode().rstrip("=")}


def _part(mime, text, charset="utf-8", headers=None, **extra):
    return {"mimeType": mime, "headers": headers or [], "body": _data(text, charset), **extra}


HTML = (
    "<html><head><title>T</title><style>p{color:red}</style></head><body>"
    "<script>var x=1;</script><p>Hello <a href='http://x'>world</a></p>"
    "<div>Second &amp; last</div></body></html>"
)


def test_html_only_top_level():
    out = _extract_body(_part("text/html", HTML))
    assert "Hello world" in out and "Second & last" in out
    assert "<" not in out and "var x" not in out and "color:red" not in out
    assert out.startswith("Hello")


def test_alternative_prefers_plain():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [_part("text/html", HTML), _part("text/plain", "plain body")],
    }
    assert _extract_body(payload) == "plain body"


def test_nested_html_only():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [_part("text/html", HTML)],
            }
        ],
    }
    assert "Hello world" in _extract_body(payload)


def test_charset_header_honoured():
    part = _part(
        "text/plain",
        "Čestitke, šola",
        "iso-8859-2",
        headers=[{"name": "content-type", "value": 'text/plain; charset="iso-8859-2"'}],
    )
    assert _extract_body(part) == "Čestitke, šola"


def test_unknown_charset_falls_back_to_utf8():
    part = _part(
        "text/plain",
        "héllo",
        headers=[{"name": "Content-Type", "value": "text/plain; charset=x-bogus"}],
    )
    assert _extract_body(part) == "héllo"


def test_attachment_ignored():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            _part("text/plain", "attached", filename="a.txt"),
            {"mimeType": "text/plain", "body": {"attachmentId": "x", "size": 9}},
            _part("text/plain", "real body"),
        ],
    }
    assert _extract_body(payload) == "real body"
