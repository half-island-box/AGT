"""Artifact content response headers must remain HTTP-safe."""

from types import SimpleNamespace
from urllib.parse import unquote

from agt.server.routes.artifacts import _content_response


def test_content_disposition_supports_unicode_filename() -> None:
    artifact = SimpleNamespace(name="报告/需求分析.md", mime_type="text/markdown")

    response = _content_response(b"hello", artifact)
    disposition = response.headers["content-disposition"]

    # Starlette must be able to encode every response header as Latin-1.
    disposition.encode("latin-1")
    assert 'filename="需求分析.md"' not in disposition
    assert "filename*=UTF-8''" in disposition
    encoded = disposition.split("filename*=UTF-8''", 1)[1]
    assert unquote(encoded) == "需求分析.md"


def test_content_disposition_sanitizes_ascii_fallback() -> None:
    artifact = SimpleNamespace(name='bad"name.txt', mime_type="text/plain")

    response = _content_response(b"body", artifact)
    disposition = response.headers["content-disposition"]

    assert 'filename="bad_name.txt"' in disposition
    assert unquote(disposition.split("filename*=UTF-8''", 1)[1]) == 'bad"name.txt'
