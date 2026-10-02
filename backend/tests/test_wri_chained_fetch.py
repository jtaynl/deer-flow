"""Unit tests for the fork-owned chained ``web_fetch`` ladder (Jina → Firecrawl → Tavily extract).

Everything is faked — ``JinaClient.crawl``, the readability extractor, the Firecrawl client factory and the
Tavily client factory — so no test touches the network. The two "identical to the Jina tool" tests run the
REAL readability extractor through both tools on the same fake crawl.
"""

from __future__ import annotations

import asyncio
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

import deerflow.community.wri_chained_fetch.tools as chained
from deerflow.community.jina_ai.jina_client import JinaClient
from deerflow.community.jina_ai.tools import web_fetch_tool as jina_web_fetch_tool
from deerflow.community.wri_chained_fetch.tools import EXHAUSTED_PREFIX, LadderConfig, _aclose_firecrawl_client, _coerce_tiers, web_fetch_tool
from deerflow.config.tool_config import ToolConfig
from deerflow.utils.readability import Article

LOGGER = "deerflow.community.wri_chained_fetch.tools"
URL = "https://example.com/report?token=sig-123"
SENTENCE = "Rice is a staple for most of the planet, and the difference between good rice and great rice is almost always technique rather than equipment. "
LONG_TEXT = (SENTENCE * 8).strip()  # ~1,100 chars: clears the 500-char floor, under the 2,500-char shell ceiling
VERY_LONG_TEXT = (SENTENCE * 25).strip()  # > 2,500 chars: never a shell, whatever it mentions
FIRECRAWL_KEY = "fc-secret-key-0123456789"
TAVILY_KEY = "tvly-secret-key-0123456789"
JINA_READ_TIMEOUT = "Error: Request to Jina API failed: ReadTimeout: The read operation timed out"


def html_page(text: str, title: str = "Report") -> str:
    return f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1><p>{text}</p></article></body></html>"


def expected_document(text: str, title: str = "Report") -> str:
    """What the fake extractor + ``Article.to_markdown`` produce for ``html_page(text, title)``."""
    return Article(title=title, html_content=f"<p>{text}</p>").to_markdown()


def tavily_ok(text: str = LONG_TEXT, title: str | None = "Tavily Page") -> dict:
    result = {"url": URL, "raw_content": text}
    if title is not None:
        result["title"] = title
    return {"results": [result], "failed_results": []}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("FIRECRAWL_API_KEY", "TAVILY_API_KEY", "JINA_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def configure(monkeypatch):
    """Install a real ``ToolConfig`` for ``web_fetch`` (defaults immediately; call it again to override)."""

    def _apply(**extra):
        tool_config = ToolConfig(name="web_fetch", group="web", use="deerflow.community.wri_chained_fetch.tools:web_fetch_tool", **extra)
        app_config = SimpleNamespace(get_tool_config=lambda name: tool_config if name == "web_fetch" else None)
        monkeypatch.setattr(chained, "get_app_config", lambda: app_config)
        return tool_config

    _apply()
    return _apply


@pytest.fixture
def jina(monkeypatch):
    """Fake ``JinaClient.crawl``: set ``.response`` (a str, or an exception to raise); ``.calls`` records kwargs."""
    state = SimpleNamespace(response=html_page(LONG_TEXT), calls=[])

    async def crawl(self, url, **kwargs):
        state.calls.append({"url": url, **kwargs})
        if isinstance(state.response, BaseException):
            raise state.response
        return state.response

    monkeypatch.setattr(JinaClient, "crawl", crawl)
    return state


@pytest.fixture
def extractor(monkeypatch):
    """Deterministic stand-in for Readability: title from <title>, content from the first <p>."""

    def extract_article(html, *, url=None):
        title = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
        paragraph = re.search(r"<p>(.*?)</p>", html, re.DOTALL)
        return Article(title=title.group(1) if title else "Untitled", html_content=f"<p>{paragraph.group(1)}</p>" if paragraph else "", url=url)

    monkeypatch.setattr(chained.readability_extractor, "extract_article", extract_article)


class FakeFirecrawl:
    def __init__(self, *, markdown: str = LONG_TEXT, title: str | None = "Fetched Page", status_code: int | None = None, error: Exception | None = None, slow: float = 0.0):
        self.calls: list[tuple[str, dict]] = []
        self.close = AsyncMock()
        self._v2_client = SimpleNamespace(async_http_client=SimpleNamespace(close=self.close))

        async def scrape(url, **kwargs):
            self.calls.append((url, kwargs))
            if slow:
                await asyncio.sleep(slow)
            if error is not None:
                raise error
            return SimpleNamespace(markdown=markdown, metadata=SimpleNamespace(title=title, status_code=status_code))

        self.scrape = scrape


@pytest.fixture
def firecrawl(monkeypatch):
    """Swap the Firecrawl client factory for a fake: ``.client`` is handed out, ``.factory_calls`` records kwargs."""
    state = SimpleNamespace(client=FakeFirecrawl(), factory_calls=[])

    def factory(api_key, api_url):
        state.factory_calls.append({"api_key": api_key, "api_url": api_url})
        return state.client

    monkeypatch.setattr(chained, "_get_firecrawl_client", factory)
    return state


class FakeTavily:
    def __init__(self, *, response: dict | None = None, error: Exception | None = None):
        self.extract = AsyncMock(return_value=tavily_ok() if response is None else response, side_effect=error)
        self.close = AsyncMock()


@pytest.fixture
def tavily(monkeypatch):
    state = SimpleNamespace(client=FakeTavily(), factory_calls=[])

    def factory(api_key):
        state.factory_calls.append({"api_key": api_key})
        return state.client

    monkeypatch.setattr(chained, "_get_tavily_client", factory)
    return state


@pytest.fixture
def ladder(configure, jina, extractor, firecrawl, tavily, monkeypatch):
    """The whole ladder faked, both fallback keys in the environment."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", FIRECRAWL_KEY)
    monkeypatch.setenv("TAVILY_API_KEY", TAVILY_KEY)
    return SimpleNamespace(configure=configure, jina=jina, firecrawl=firecrawl, tavily=tavily)


async def fetch(url: str = URL) -> str:
    return await web_fetch_tool.ainvoke({"url": url})


# ---------------------------------------------------------------------------
# the model-visible contract
# ---------------------------------------------------------------------------


def test_schema_contract_equals_the_jina_tool():
    assert web_fetch_tool.name == jina_web_fetch_tool.name == "web_fetch"
    assert web_fetch_tool.description == jina_web_fetch_tool.description
    assert web_fetch_tool.args_schema.model_json_schema() == jina_web_fetch_tool.args_schema.model_json_schema()
    assert convert_to_openai_tool(web_fetch_tool) == convert_to_openai_tool(jina_web_fetch_tool)


def test_tool_config_schema_allows_the_ladder_keys():
    config = ToolConfig(
        name="web_fetch",
        group="web",
        use="deerflow.community.wri_chained_fetch.tools:web_fetch_tool",
        timeout=120,
        fallback_timeout=60,
        min_content_chars=400,
        thin_policy="error",
        max_chars=8192,
        tiers=["jina", "firecrawl"],
        firecrawl_api_key="x",
        firecrawl_base_url="http://fc.local:3002",
    )
    assert config.model_extra == {
        "timeout": 120,
        "fallback_timeout": 60,
        "min_content_chars": 400,
        "thin_policy": "error",
        "max_chars": 8192,
        "tiers": ["jina", "firecrawl"],
        "firecrawl_api_key": "x",
        "firecrawl_base_url": "http://fc.local:3002",
    }


# ---------------------------------------------------------------------------
# the Jina rung
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_jina_ok_no_fallback_and_pools_untouched(ladder):
    result = await fetch()

    assert result == expected_document(LONG_TEXT)
    assert ladder.firecrawl.factory_calls == []
    assert ladder.tavily.factory_calls == []
    ladder.firecrawl.client.close.assert_not_awaited()
    ladder.tavily.client.close.assert_not_awaited()


@pytest.mark.anyio
async def test_jina_request_defaults_match_the_spec(ladder):
    await fetch()

    assert ladder.jina.calls == [{"url": URL, "return_format": "html", "timeout": 60, "proxy": None, "trust_env": True}]


@pytest.mark.anyio
async def test_jina_request_honours_timeout_proxy_and_trust_env(ladder):
    ladder.configure(timeout="20", proxy="http://host.docker.internal:7890", trust_env="false")

    await fetch()

    assert ladder.jina.calls == [{"url": URL, "return_format": "html", "timeout": 20, "proxy": "http://host.docker.internal:7890", "trust_env": False}]


@pytest.mark.anyio
async def test_jina_empty_proxy_from_an_unresolved_env_var_is_dropped(ladder):
    ladder.configure(proxy="   ", trust_env=True)

    await fetch()

    assert ladder.jina.calls[0]["proxy"] is None


@pytest.mark.anyio
async def test_readability_is_offloaded_to_a_thread(ladder, monkeypatch):
    seen: list[object] = []
    original_to_thread = asyncio.to_thread

    async def tracking_to_thread(func, *args, **kwargs):
        seen.append(func)
        return await original_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(chained.asyncio, "to_thread", tracking_to_thread)

    result = await fetch()

    assert result == expected_document(LONG_TEXT)
    assert seen == [chained.readability_extractor.extract_article]


@pytest.mark.anyio
async def test_jina_error_string_falls_back_to_firecrawl_and_closes_its_pool(ladder, caplog):
    ladder.jina.response = JINA_READ_TIMEOUT

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Fetched Page\n\n{LONG_TEXT}"
    assert ladder.firecrawl.factory_calls == [{"api_key": FIRECRAWL_KEY, "api_url": None}]
    assert ladder.firecrawl.client.calls == [(URL, {"formats": ["markdown"]})]
    ladder.firecrawl.client.close.assert_awaited_once()
    assert ladder.tavily.factory_calls == []
    assert "tier=jina outcome=fail reason=Request to Jina API failed: ReadTimeout: The read operation timed out" in caplog.text
    assert "tier=firecrawl outcome=ok" in caplog.text


@pytest.mark.anyio
async def test_jina_thin_content_falls_back_to_firecrawl(ladder, caplog):
    ladder.jina.response = html_page("x" * 120)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Fetched Page\n\n{LONG_TEXT}"
    assert re.search(r"tier=jina outcome=fail reason=thin content \(\d+ chars < min_content_chars 500\)", caplog.text)


@pytest.mark.anyio
async def test_jina_exception_is_a_rung_failure_not_a_tool_crash(ladder):
    ladder.jina.response = RuntimeError("socket exploded")

    result = await fetch()

    assert result == f"# Fetched Page\n\n{LONG_TEXT}"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param(html_page("Enable JavaScript and cookies to continue", title="Just a moment..."), "challenge/error shell ('just a moment')", id="cloudflare-interstitial"),
        pytest.param(html_page("Please stand by", title="Attention Required! | Cloudflare"), "challenge/error shell ('attention required')", id="cloudflare-block"),
        pytest.param(html_page("You don't have permission to access this resource.", title="Access Denied"), "challenge/error shell ('access denied')", id="akamai"),
        pytest.param(html_page("Request blocked. The request could not be satisfied. Generated by cloudfront", title="ERROR"), "challenge/error shell ('request blocked')", id="cloudfront"),
        pytest.param(html_page("Please complete the captcha below to continue.", title="Security check"), "challenge/error shell ('captcha')", id="captcha"),
        pytest.param(html_page("nginx", title="403 Forbidden"), "challenge/error shell ('403 forbidden')", id="bare-403"),
        pytest.param(html_page("Performance & security by Cloudflare", title="Ray ID"), "challenge/error shell ('cloudflare')", id="cloudflare-tiny-body"),
        pytest.param(
            html_page("www.singstat.gov.sg needs to review the security of your connection before proceeding.", title="www.singstat.gov.sg"),
            "challenge/error shell ('review the security of your connection')",
            id="cloudflare-body-without-title",
        ),
        pytest.param(html_page("Verifying you are human. This may take a few seconds.", title="www.singstat.gov.sg"), "challenge/error shell ('you are human')", id="turnstile"),
        pytest.param("Title: ERROR: Forbidden\n\nURL Source: https://example.com/report\n\nWarning: Target URL returned error 403: Forbidden\n", "jina error shell (target returned 403)", id="jina-200-error-shell"),
        pytest.param("Title: ERROR: upstream failed\n\nMarkdown Content:\n", "jina error shell (Title: ERROR)", id="jina-title-error-shell"),
    ],
)
@pytest.mark.anyio
async def test_jina_shell_with_a_200_is_never_content(ladder, caplog, body, reason):
    ladder.jina.response = body

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Fetched Page\n\n{LONG_TEXT}"
    assert f"tier=jina outcome=fail reason={reason} url=" in caplog.text


@pytest.mark.anyio
async def test_long_page_mentioning_access_denied_is_content(ladder):
    ladder.jina.response = html_page(VERY_LONG_TEXT + " The ministry's statement said 'Access Denied' is what callers saw; Cloudflare and a captcha were involved.")

    result = await fetch()

    assert result.startswith("# Report\n\nRice is a staple")
    assert ladder.firecrawl.factory_calls == []


# ---------------------------------------------------------------------------
# the Firecrawl rung
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_firecrawl_without_a_key_is_skipped_to_tavily(ladder, caplog, monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    ladder.jina.response = JINA_READ_TIMEOUT

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Tavily Page\n\n{LONG_TEXT}"
    assert ladder.firecrawl.factory_calls == []
    assert ladder.tavily.factory_calls == [{"api_key": TAVILY_KEY}]
    assert "tier=firecrawl outcome=fail reason=skipped (FIRECRAWL_API_KEY not set and no firecrawl_api_key configured)" in caplog.text


@pytest.mark.anyio
async def test_firecrawl_key_and_base_url_from_the_tool_config(ladder, monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    ladder.configure(firecrawl_api_key="fc-configured-key-9876", firecrawl_base_url="http://192.168.0.47:3002")
    ladder.jina.response = JINA_READ_TIMEOUT

    result = await fetch()

    assert result == f"# Fetched Page\n\n{LONG_TEXT}"
    assert ladder.firecrawl.factory_calls == [{"api_key": "fc-configured-key-9876", "api_url": "http://192.168.0.47:3002"}]


@pytest.mark.anyio
async def test_firecrawl_raises_falls_back_to_tavily_and_still_closes_its_pool(ladder, caplog):
    ladder.jina.response = JINA_READ_TIMEOUT
    ladder.firecrawl.client = FakeFirecrawl(error=RuntimeError("scrape failed"))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Tavily Page\n\n{LONG_TEXT}"
    ladder.firecrawl.client.close.assert_awaited_once()
    ladder.tavily.client.close.assert_awaited_once()
    assert "tier=firecrawl outcome=fail reason=RuntimeError: scrape failed" in caplog.text


@pytest.mark.parametrize(
    ("markdown", "title", "reason"),
    [
        pytest.param("", "Fetched Page", "no content found", id="empty-markdown"),
        pytest.param("   \n  ", "Fetched Page", "no content found", id="whitespace-markdown"),
        pytest.param("Only a nav shell.", "Fetched Page", "thin content (17 chars < min_content_chars 500)", id="thin-markdown"),
        pytest.param("Enable JavaScript and cookies to continue", "Just a moment...", "challenge/error shell ('just a moment')", id="shell-markdown"),
    ],
)
@pytest.mark.anyio
async def test_firecrawl_thin_or_shell_result_falls_back_to_tavily(ladder, caplog, markdown, title, reason):
    ladder.jina.response = JINA_READ_TIMEOUT
    ladder.firecrawl.client = FakeFirecrawl(markdown=markdown, title=title)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Tavily Page\n\n{LONG_TEXT}"
    assert f"tier=firecrawl outcome=fail reason={reason} url=" in caplog.text
    ladder.firecrawl.client.close.assert_awaited_once()


@pytest.mark.anyio
async def test_firecrawl_rung_is_bounded_by_the_timeout(ladder, caplog):
    ladder.configure(timeout=1)
    ladder.jina.response = JINA_READ_TIMEOUT
    ladder.firecrawl.client = FakeFirecrawl(slow=5)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Tavily Page\n\n{LONG_TEXT}"
    assert "tier=firecrawl outcome=fail reason=timed out after 1s" in caplog.text
    ladder.firecrawl.client.close.assert_awaited_once()


@pytest.mark.anyio
async def test_firecrawl_pool_close_failure_never_masks_its_result(ladder, caplog):
    ladder.jina.response = JINA_READ_TIMEOUT
    ladder.firecrawl.client.close.side_effect = RuntimeError("close failed")

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Fetched Page\n\n{LONG_TEXT}"
    ladder.firecrawl.client.close.assert_awaited_once()
    assert "Failed to close the Firecrawl async HTTP pool" in caplog.text
    assert ladder.tavily.factory_calls == []


@pytest.mark.anyio
async def test_firecrawl_untitled_when_metadata_has_no_title(ladder):
    ladder.jina.response = JINA_READ_TIMEOUT
    ladder.firecrawl.client = FakeFirecrawl(title=None)

    result = await fetch()

    assert result == f"# Untitled\n\n{LONG_TEXT}"


@pytest.mark.parametrize("status", [403, 404, 429, 500])
@pytest.mark.anyio
async def test_firecrawl_4xx_5xx_target_is_never_content_however_long(ladder, caplog, status):
    """firecrawl-py reports the target's own status in ``metadata.status_code``; a branded error page is long
    enough to clear both text gates, so the status is the gate (review finding 2)."""
    ladder.jina.response = JINA_READ_TIMEOUT
    ladder.firecrawl.client = FakeFirecrawl(markdown="Sorry, the page you are looking for cannot be found. " + VERY_LONG_TEXT, title="Page not found", status_code=status)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Tavily Page\n\n{LONG_TEXT}"
    assert f"tier=firecrawl outcome=fail reason=target returned {status} url=" in caplog.text
    ladder.firecrawl.client.close.assert_awaited_once()


@pytest.mark.parametrize("status", [None, 200, 304, True], ids=["absent", "200", "304", "bool-is-not-a-status"])
@pytest.mark.anyio
async def test_firecrawl_status_below_400_or_absent_is_content(ladder, status):
    ladder.jina.response = JINA_READ_TIMEOUT
    ladder.firecrawl.client = FakeFirecrawl(status_code=status)

    result = await fetch()

    assert result == f"# Fetched Page\n\n{LONG_TEXT}"


@pytest.mark.anyio
async def test_firecrawl_sdk_document_carries_the_target_status(ladder, caplog):
    """The locked firecrawl-py maps the API's ``statusCode`` onto ``Document.metadata.status_code`` (no network)."""
    from firecrawl.v2.types import Document
    from firecrawl.v2.utils.normalize import normalize_document_input

    document = Document(**normalize_document_input({"markdown": VERY_LONG_TEXT, "metadata": {"title": "Page not found", "statusCode": 404, "sourceURL": URL}}))
    assert document.metadata is not None and document.metadata.status_code == 404
    ladder.jina.response = JINA_READ_TIMEOUT

    async def scrape(url, **kwargs):
        return document

    ladder.firecrawl.client.scrape = scrape

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Tavily Page\n\n{LONG_TEXT}"
    assert "tier=firecrawl outcome=fail reason=target returned 404 url=" in caplog.text


@pytest.mark.anyio
async def test_fallback_timeout_bounds_the_fallback_rungs_but_not_jina(ladder, caplog):
    """``fallback_timeout`` decouples Firecrawl/Tavily from the Jina budget (review finding 5)."""
    ladder.configure(timeout=20, fallback_timeout=1)
    ladder.jina.response = JINA_READ_TIMEOUT
    ladder.firecrawl.client = FakeFirecrawl(slow=5)

    async def slow_extract(urls):
        await asyncio.sleep(5)
        return tavily_ok()

    ladder.tavily.client.extract = slow_extract

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert ladder.jina.calls[0]["timeout"] == 20
    assert result == EXHAUSTED_PREFIX + "jina: Request to Jina API failed: ReadTimeout: The read operation timed out; firecrawl: timed out after 1s; tavily: timed out after 1s"
    assert "tier=firecrawl outcome=fail reason=timed out after 1s" in caplog.text
    assert "tier=tavily outcome=fail reason=timed out after 1s" in caplog.text
    ladder.firecrawl.client.close.assert_awaited_once()
    ladder.tavily.client.close.assert_awaited_once()


def test_fallback_timeout_defaults_to_timeout(configure):
    configure(timeout=120)
    assert chained._load_config().fallback_timeout == 120

    configure(timeout=120, fallback_timeout=60)
    assert chained._load_config().fallback_timeout == 60

    configure(timeout=120, fallback_timeout="later")
    assert chained._load_config().fallback_timeout == 120


@pytest.mark.anyio
async def test_the_tavily_sdk_timeout_is_a_reason_of_its_own(ladder):
    """tavily-python caps ``extract`` at 30 s itself and raises its own (non-builtin) TimeoutError."""
    from tavily.errors import TimeoutError as TavilyTimeoutError

    ladder.configure(tiers=["tavily"], fallback_timeout=60)
    ladder.tavily.client = FakeTavily(error=TavilyTimeoutError(30))

    result = await fetch()

    assert result == EXHAUSTED_PREFIX + "tavily: TimeoutError: Request timed out after 30 seconds.; jina: disabled; firecrawl: disabled"
    ladder.tavily.client.close.assert_awaited_once()


class TestFirecrawlTeardown:
    """Mirror of upstream #6013 (``5312271f``): best-effort, never raises, tolerates other SDK shapes."""

    @pytest.mark.anyio
    async def test_reports_a_pool_without_close(self, caplog):
        client = SimpleNamespace(_v2_client=SimpleNamespace(async_http_client=SimpleNamespace()))

        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await _aclose_firecrawl_client(client)

        assert "Firecrawl async HTTP pool has no close method" in caplog.text

    @pytest.mark.anyio
    async def test_accepts_a_synchronous_close(self, caplog):
        close = Mock()
        client = SimpleNamespace(_v2_client=SimpleNamespace(async_http_client=SimpleNamespace(close=close)))

        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            await _aclose_firecrawl_client(client)

        close.assert_called_once_with()
        assert "Failed to close" not in caplog.text

    @pytest.mark.anyio
    async def test_is_a_no_op_without_the_v2_client(self):
        await _aclose_firecrawl_client(SimpleNamespace())
        await _aclose_firecrawl_client(SimpleNamespace(_v2_client=None))

    @pytest.mark.anyio
    async def test_closes_the_real_sdk_pool(self):
        from firecrawl import AsyncFirecrawlApp as RealApp

        client = RealApp(api_key="test-key")
        pooled = getattr(getattr(client, "_v2_client", None), "async_http_client", None)
        inner = getattr(pooled, "_client", None)
        if inner is None:
            pytest.skip("locked firecrawl-py private pool layout not present")

        assert inner.is_closed is False

        await _aclose_firecrawl_client(client)

        assert inner.is_closed is True


# ---------------------------------------------------------------------------
# the Tavily rung
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_tavily_without_a_key_is_skipped(ladder, caplog, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY")
    ladder.jina.response = JINA_READ_TIMEOUT
    ladder.firecrawl.client = FakeFirecrawl(error=RuntimeError("scrape failed"))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == EXHAUSTED_PREFIX + "jina: Request to Jina API failed: ReadTimeout: The read operation timed out; firecrawl: RuntimeError: scrape failed; tavily: skipped (TAVILY_API_KEY not set and no tavily_api_key configured)"
    assert ladder.tavily.factory_calls == []


@pytest.mark.anyio
async def test_tavily_key_from_the_tool_config(ladder, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY")
    ladder.configure(tiers=["tavily"], tavily_api_key="tvly-configured-key-9876")

    result = await fetch()

    assert result == f"# Tavily Page\n\n{LONG_TEXT}"
    assert ladder.tavily.factory_calls == [{"api_key": "tvly-configured-key-9876"}]
    ladder.tavily.client.extract.assert_awaited_once_with([URL])
    ladder.tavily.client.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        pytest.param({"results": [], "failed_results": [{"url": URL, "error": "Failed to fetch URL"}]}, "extract failed: Failed to fetch URL", id="failed-results"),
        pytest.param({"results": [], "failed_results": []}, "no results found", id="no-results"),
        pytest.param(tavily_ok(""), "no content found", id="empty-raw-content"),
        pytest.param(tavily_ok("Home | About | Contact"), "thin content (22 chars < min_content_chars 500)", id="nav-shell"),
        pytest.param(tavily_ok("Just a moment... Enable JavaScript and cookies to continue", title="Just a moment..."), "challenge/error shell ('just a moment')", id="challenge"),
    ],
)
@pytest.mark.anyio
async def test_tavily_failures_are_reasons(ladder, caplog, response, reason):
    ladder.configure(tiers=["tavily"], thin_policy="error")
    ladder.tavily.client = FakeTavily(response=response)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == EXHAUSTED_PREFIX + f"tavily: {reason}; jina: disabled; firecrawl: disabled"
    assert f"tier=tavily outcome=fail reason={reason} url=" in caplog.text
    ladder.tavily.client.close.assert_awaited_once()


@pytest.mark.anyio
async def test_tavily_title_falls_back_to_the_url(ladder):
    ladder.configure(tiers=["tavily"])
    ladder.tavily.client = FakeTavily(response=tavily_ok(title=None))

    result = await fetch()

    assert result == f"# {URL}\n\n{LONG_TEXT}"


@pytest.mark.anyio
async def test_tavily_client_is_closed_even_when_extract_raises(ladder, caplog):
    ladder.configure(tiers=["tavily"])
    ladder.tavily.client = FakeTavily(error=RuntimeError("extract failed"))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == EXHAUSTED_PREFIX + "tavily: RuntimeError: extract failed; jina: disabled; firecrawl: disabled"
    ladder.tavily.client.close.assert_awaited_once()


@pytest.mark.anyio
async def test_tavily_rung_is_bounded_by_the_timeout(ladder):
    ladder.configure(tiers=["tavily"], timeout=1)

    async def slow_extract(urls):
        await asyncio.sleep(5)
        return tavily_ok()

    ladder.tavily.client.extract = slow_extract

    result = await fetch()

    assert result == EXHAUSTED_PREFIX + "tavily: timed out after 1s; jina: disabled; firecrawl: disabled"
    ladder.tavily.client.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# the ladder as a whole
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_exhausted_names_all_three_reasons(ladder, caplog):
    ladder.jina.response = JINA_READ_TIMEOUT
    ladder.firecrawl.client = FakeFirecrawl(error=RuntimeError("scrape failed"))
    ladder.tavily.client = FakeTavily(response={"results": [], "failed_results": [{"url": URL, "error": "Failed to fetch URL"}]})

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == EXHAUSTED_PREFIX + "jina: Request to Jina API failed: ReadTimeout: The read operation timed out; firecrawl: RuntimeError: scrape failed; tavily: extract failed: Failed to fetch URL"
    assert result.startswith("Error:")
    assert "web_fetch ladder exhausted — jina: Request to Jina API failed" in caplog.text
    ladder.firecrawl.client.close.assert_awaited_once()
    ladder.tavily.client.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# thin_policy — every attempted rung thin, nothing else wrong
# ---------------------------------------------------------------------------

THIN_JINA = "Home | About | Contact"
THIN_FIRECRAWL = "Release calendar — the Q3 bulletin moves to 14 October; the Q4 bulletin date is unchanged. Enquiries: the statistics desk."
THIN_TAVILY = "Release calendar: the Q3 bulletin moves to 14 October."


@pytest.mark.anyio
async def test_every_attempted_rung_thin_returns_the_longest_thin_page(ladder, caplog):
    """Three readers agree the page is short and none saw a shell, an error or a timeout: a short page, not a shell
    (review finding 3, option b) — the longest wins, the ladder is logged as usual plus one ``outcome=thin`` line."""
    ladder.jina.response = html_page(THIN_JINA)
    ladder.firecrawl.client = FakeFirecrawl(markdown=THIN_FIRECRAWL)
    ladder.tavily.client = FakeTavily(response=tavily_ok(THIN_TAVILY))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Fetched Page\n\n{THIN_FIRECRAWL}"
    assert caplog.text.count("outcome=fail reason=thin content (") == 3
    assert f"tier=firecrawl outcome=thin reason=every attempted rung thin; returning the longest ({len(THIN_FIRECRAWL)} chars) url=https://example.com/report" in caplog.text
    assert "ladder exhausted" not in caplog.text
    assert SENTENCE.strip() not in caplog.text and THIN_FIRECRAWL not in caplog.text
    ladder.firecrawl.client.close.assert_awaited_once()
    ladder.tavily.client.close.assert_awaited_once()


@pytest.mark.anyio
async def test_thin_policy_error_keeps_the_exhausted_error(ladder):
    """``thin_policy: error`` restores the strict rule — a thin page at every rung is an error and the browser is next."""
    ladder.configure(thin_policy="error")
    ladder.jina.response = html_page(THIN_JINA)
    ladder.firecrawl.client = FakeFirecrawl(markdown=THIN_FIRECRAWL)
    ladder.tavily.client = FakeTavily(response=tavily_ok(THIN_TAVILY))

    result = await fetch()

    assert result.startswith(EXHAUSTED_PREFIX)
    assert result.count("thin content (") == 3


@pytest.mark.parametrize(
    ("jina_response", "make_firecrawl", "blocking_reason"),
    [
        pytest.param(html_page(THIN_JINA), lambda: FakeFirecrawl(markdown="Enable JavaScript and cookies to continue", title="Just a moment..."), "firecrawl: challenge/error shell ('just a moment')", id="shell-at-one-rung"),
        pytest.param(JINA_READ_TIMEOUT, lambda: FakeFirecrawl(markdown=THIN_FIRECRAWL), "jina: Request to Jina API failed: ReadTimeout", id="transport-error-at-one-rung"),
        pytest.param(html_page(THIN_JINA), lambda: FakeFirecrawl(markdown=VERY_LONG_TEXT, status_code=404), "firecrawl: target returned 404", id="4xx-at-one-rung"),
        pytest.param(html_page(THIN_JINA), lambda: FakeFirecrawl(error=RuntimeError("scrape failed")), "firecrawl: RuntimeError: scrape failed", id="exception-at-one-rung"),
    ],
)
@pytest.mark.anyio
async def test_a_shell_or_error_at_any_attempted_rung_keeps_the_exhausted_error(ladder, jina_response, make_firecrawl, blocking_reason):
    ladder.jina.response = jina_response
    ladder.firecrawl.client = make_firecrawl()
    ladder.tavily.client = FakeTavily(response=tavily_ok(THIN_TAVILY))

    result = await fetch()

    assert result.startswith(EXHAUSTED_PREFIX)
    assert blocking_reason in result
    assert "tavily: thin content (" in result


@pytest.mark.anyio
async def test_skipped_and_disabled_rungs_do_not_block_the_thin_return(ladder, monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    ladder.configure(tiers=["jina", "firecrawl"])
    ladder.jina.response = html_page(THIN_JINA)

    result = await fetch()

    assert result == expected_document(THIN_JINA)
    assert ladder.firecrawl.factory_calls == []
    assert ladder.tavily.factory_calls == []


@pytest.mark.anyio
async def test_the_thin_return_is_capped_by_max_chars(ladder):
    ladder.configure(max_chars=24)
    ladder.jina.response = html_page(THIN_JINA)
    ladder.firecrawl.client = FakeFirecrawl(markdown=THIN_FIRECRAWL)
    ladder.tavily.client = FakeTavily(response=tavily_ok(THIN_TAVILY))

    result = await fetch()

    assert result == f"# Fetched Page\n\n{THIN_FIRECRAWL}"[:24]


@pytest.mark.anyio
async def test_an_unknown_thin_policy_falls_back_to_longest(ladder, caplog):
    ladder.configure(thin_policy="maybe")
    ladder.jina.response = html_page(THIN_JINA)
    ladder.firecrawl.client = FakeFirecrawl(markdown=THIN_FIRECRAWL)
    ladder.tavily.client = FakeTavily(response=tavily_ok(THIN_TAVILY))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = await fetch()

    assert result == f"# Fetched Page\n\n{THIN_FIRECRAWL}"
    assert "ignoring unknown 'thin_policy' config 'maybe'; using 'longest'" in caplog.text


@pytest.mark.anyio
async def test_tiers_config_disables_firecrawl(ladder, caplog):
    ladder.configure(tiers=["jina", "tavily"])
    ladder.jina.response = JINA_READ_TIMEOUT

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Tavily Page\n\n{LONG_TEXT}"
    assert ladder.firecrawl.factory_calls == []
    assert "tier=firecrawl" not in caplog.text

    ladder.tavily.client = FakeTavily(error=RuntimeError("extract failed"))
    result = await fetch()

    assert result == EXHAUSTED_PREFIX + "jina: Request to Jina API failed: ReadTimeout: The read operation timed out; tavily: RuntimeError: extract failed; firecrawl: disabled"


@pytest.mark.anyio
async def test_tiers_config_accepts_a_comma_separated_string(ladder):
    ladder.configure(tiers="jina, tavily")
    ladder.jina.response = JINA_READ_TIMEOUT

    result = await fetch()

    assert result == f"# Tavily Page\n\n{LONG_TEXT}"
    assert ladder.firecrawl.factory_calls == []


def test_coerce_tiers(caplog):
    assert _coerce_tiers(None) == ("jina", "firecrawl", "tavily")
    assert _coerce_tiers(["jina"]) == ("jina",)
    assert _coerce_tiers(["Tavily", "jina", "jina"]) == ("tavily", "jina")
    assert _coerce_tiers("firecrawl tavily") == ("firecrawl", "tavily")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert _coerce_tiers(["jina", "scrapingbee"]) == ("jina",)
        assert _coerce_tiers([]) == ("jina", "firecrawl", "tavily")
        assert _coerce_tiers(42) == ("jina", "firecrawl", "tavily")
    assert "ignoring unknown tier 'scrapingbee'" in caplog.text
    assert "enables no known tier" in caplog.text
    assert "unusable 'tiers' config of type int" in caplog.text


@pytest.mark.anyio
async def test_max_chars_caps_the_document(ladder):
    ladder.configure(max_chars=50)

    result = await fetch()

    assert result == expected_document(LONG_TEXT)[:50]
    assert len(result) == 50


@pytest.mark.anyio
async def test_default_cap_is_4096_like_the_stock_tools(ladder):
    ladder.jina.response = html_page(VERY_LONG_TEXT * 3)

    result = await fetch()

    assert len(result) == 4096
    assert result == expected_document(VERY_LONG_TEXT * 3)[:4096]


@pytest.mark.anyio
async def test_min_content_chars_is_configurable_and_zero_disables_the_floor(ladder):
    ladder.configure(min_content_chars=10)
    ladder.jina.response = html_page("x" * 120)
    assert (await fetch()) == expected_document("x" * 120)

    ladder.configure(min_content_chars=0)
    ladder.jina.response = html_page("x")
    assert (await fetch()) == expected_document("x")
    assert ladder.firecrawl.factory_calls == []


@pytest.mark.anyio
async def test_invalid_config_values_fall_back_to_defaults(ladder):
    ladder.configure(timeout="soon", fallback_timeout="later", min_content_chars=-5, thin_policy=42, max_chars=0, trust_env="maybe")

    await fetch()

    assert ladder.jina.calls[0]["timeout"] == 60
    assert ladder.jina.calls[0]["trust_env"] is True
    assert chained._load_config() == LadderConfig()


@pytest.mark.anyio
async def test_logs_carry_tier_and_reason_but_never_content_keys_or_the_query_string(ladder, caplog):
    ladder.jina.response = JINA_READ_TIMEOUT

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == f"# Fetched Page\n\n{LONG_TEXT}"
    assert "tier=jina outcome=fail" in caplog.text
    assert "tier=firecrawl outcome=ok reason=ok (" in caplog.text
    assert "url=https://example.com/report" in caplog.text
    assert "token=sig-123" not in caplog.text
    assert FIRECRAWL_KEY not in caplog.text
    assert TAVILY_KEY not in caplog.text
    assert SENTENCE.strip() not in caplog.text


def test_loggable_url_drops_query_fragment_and_userinfo():
    assert chained._loggable_url("https://user:pass@example.com:8443/path/page?token=sig-123#frag") == "https://example.com:8443/path/page"
    assert chained._loggable_url("https://example.com/report") == "https://example.com/report"
    assert chained._loggable_url("not a url") == "not a url"


@pytest.mark.anyio
async def test_a_key_echoed_by_an_sdk_error_is_redacted_from_the_result_and_the_logs(ladder, caplog, monkeypatch):
    monkeypatch.setenv("JINA_API_KEY", "jina-secret-key-0123456789")
    ladder.jina.response = "Error: Jina API returned status 401: bad key jina-secret-key-0123456789"
    ladder.firecrawl.client = FakeFirecrawl(error=RuntimeError(f"401 Unauthorized for {FIRECRAWL_KEY}"))
    ladder.tavily.client = FakeTavily(error=RuntimeError(f"401 Unauthorized for {TAVILY_KEY}"))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert result == EXHAUSTED_PREFIX + "jina: Jina API returned status 401: bad key ***; firecrawl: RuntimeError: 401 Unauthorized for ***; tavily: RuntimeError: 401 Unauthorized for ***"
    for secret in ("jina-secret-key-0123456789", FIRECRAWL_KEY, TAVILY_KEY):
        assert secret not in caplog.text


@pytest.mark.parametrize("decorate", [lambda key: key + "\n", lambda key: f"  {key}  ", lambda key: key + "\r\n"], ids=["trailing-newline", "padded", "crlf"])
@pytest.mark.anyio
async def test_a_key_with_whitespace_around_it_in_the_env_is_still_redacted(ladder, caplog, monkeypatch, decorate):
    """The rungs hand the SDKs the STRIPPED env value, so an echoed key comes back bare; the redaction must know
    that form too (review finding 1)."""
    monkeypatch.setenv("FIRECRAWL_API_KEY", decorate(FIRECRAWL_KEY))
    monkeypatch.setenv("TAVILY_API_KEY", decorate(TAVILY_KEY))
    ladder.jina.response = JINA_READ_TIMEOUT
    ladder.firecrawl.client = FakeFirecrawl(error=RuntimeError(f"401 Unauthorized for key {FIRECRAWL_KEY}"))
    ladder.tavily.client = FakeTavily(error=RuntimeError(f"401 Unauthorized for key {TAVILY_KEY}"))

    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await fetch()

    assert ladder.firecrawl.factory_calls == [{"api_key": FIRECRAWL_KEY, "api_url": None}]
    assert ladder.tavily.factory_calls == [{"api_key": TAVILY_KEY}]
    assert result == EXHAUSTED_PREFIX + "jina: Request to Jina API failed: ReadTimeout: The read operation timed out; firecrawl: RuntimeError: 401 Unauthorized for key ***; tavily: RuntimeError: 401 Unauthorized for key ***"
    assert FIRECRAWL_KEY not in caplog.text
    assert TAVILY_KEY not in caplog.text


def test_known_secrets_registers_the_stripped_and_the_raw_form(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", FIRECRAWL_KEY + "\n")
    monkeypatch.setenv("TAVILY_API_KEY", "short")

    secrets = chained._known_secrets(LadderConfig(tavily_api_key="tvly-configured-key-9876"))

    assert secrets == ("tvly-configured-key-9876", FIRECRAWL_KEY, FIRECRAWL_KEY + "\n")


@pytest.mark.anyio
async def test_long_reasons_are_single_line_and_truncated(ladder):
    ladder.configure(tiers=["jina"])
    ladder.jina.response = "Error: Jina API returned status 502: " + "<html>\n<body>bad gateway</body>\n</html> " * 40

    result = await fetch()

    assert "\n" not in result
    assert len(result) <= len(EXHAUSTED_PREFIX) + len("jina: ") + chained.REASON_MAX_CHARS + len("; firecrawl: disabled; tavily: disabled")
    assert result.startswith(EXHAUSTED_PREFIX + "jina: Jina API returned status 502: <html> <body>bad gateway</body> </html>")
    assert "…; firecrawl: disabled; tavily: disabled" in result


# ---------------------------------------------------------------------------
# with every fallback disabled the tool IS the Jina tool
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_jina_only_success_is_byte_identical_to_the_jina_tool(configure, jina, firecrawl, tavily, monkeypatch):
    """Real readability extraction through both tools on the same fake crawl."""
    configure(tiers=["jina"])
    jina_config = SimpleNamespace(get_tool_config=lambda name: None)
    monkeypatch.setattr("deerflow.community.jina_ai.tools.get_app_config", lambda: jina_config)
    jina.response = html_page(LONG_TEXT)

    chained_result = await fetch()
    jina_result = await jina_web_fetch_tool.ainvoke({"url": URL})

    assert chained_result == jina_result
    assert "Rice is a staple" in chained_result
    assert firecrawl.factory_calls == []
    assert tavily.factory_calls == []


@pytest.mark.anyio
async def test_jina_only_thin_page_is_byte_identical_to_the_jina_tool(configure, jina, firecrawl, tavily, monkeypatch):
    """Real readability on a short page: the single attempted rung is thin and nothing else is wrong, so the chained
    tool hands back exactly what the Jina tool hands back."""
    configure(tiers=["jina"])
    jina_config = SimpleNamespace(get_tool_config=lambda name: None)
    monkeypatch.setattr("deerflow.community.jina_ai.tools.get_app_config", lambda: jina_config)
    jina.response = html_page("The Q3 bulletin moves to 14 October; the Q4 bulletin date is unchanged.", title="Release calendar")

    chained_result = await fetch()
    jina_result = await jina_web_fetch_tool.ainvoke({"url": URL})

    assert chained_result == jina_result
    assert chained_result.startswith("# Release calendar\n\n")
    assert firecrawl.factory_calls == []
    assert tavily.factory_calls == []


@pytest.mark.anyio
async def test_jina_only_failure_is_the_exhausted_error_with_the_others_disabled(ladder):
    ladder.configure(tiers=["jina"])
    ladder.jina.response = JINA_READ_TIMEOUT

    result = await fetch()

    assert result == EXHAUSTED_PREFIX + "jina: Request to Jina API failed: ReadTimeout: The read operation timed out; firecrawl: disabled; tavily: disabled"
    assert ladder.firecrawl.factory_calls == []
    assert ladder.tavily.factory_calls == []
