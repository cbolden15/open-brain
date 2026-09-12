from __future__ import annotations

from open_brain.integrations.ports import (
    PageDocument,
    PageReadRequest,
    RedactedText,
    TrustLabel,
)
from open_brain.integrations.ui import UiHandler, UiRequest


class SyntheticPageReader:
    def __init__(self, page: PageDocument) -> None:
        self._page = page

    def read(self, request: PageReadRequest) -> PageDocument | None:
        return self._page if request.page_id == self._page.page_id else None


def test_page_markdown_and_title_are_rendered_without_executable_html() -> None:
    page = PageDocument(
        page_id="page.synthetic-001",
        title=RedactedText.redact('Synthetic <img src=x onerror="alert(1)">'),
        markdown=RedactedText.redact(
            "# Safe heading\n\n"
            "<img src=x onerror=\"alert('synthetic')\">\n\n"
            "[unsafe](javascript:alert('synthetic'))"
        ),
        trust=TrustLabel.VERIFIED_WORK,
    )
    handler = UiHandler(
        expected_bearer_token="synthetic-ui-token",
        page_reader=SyntheticPageReader(page),
    )

    response = handler.handle(
        UiRequest(
            method="GET",
            path="/pages/page.synthetic-001",
            headers=(("Authorization", "Bearer synthetic-ui-token"),),
        )
    )
    body = response.body.decode("utf-8")

    assert response.status == 200
    assert ("Content-Type", "text/html; charset=utf-8") in response.headers
    assert "<h1>Safe heading</h1>" in body
    assert "&lt;img" in body
    assert "<script" not in body.casefold()
    assert "<img" not in body.casefold()
    assert "href=\"javascript:" not in body.casefold()
    assert "Content-Security-Policy" in dict(response.headers)
