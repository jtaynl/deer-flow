r"""WRI chained ``web_fetch`` — fork-owned, additive; the ladder **Jina → Firecrawl → Tavily extract**.

Why: across the 41 PMI pilot threads of 1 Oct 2026 the agent's single-reader ``web_fetch`` (Jina) failed
9 of 190 calls; the owner-approved fetch plan (2 Oct 2026, item 3) chains the readers the gateway already
has, in the spirit of the host-side ``pmi_engine.fetch`` ladder: whenever a rung does not yield CONTENT —
for ANY reason: transport error, Jina error shell, challenge page, thin page, timeout — the next rung is
tried, every rung's result goes through the same two gates, and the first rung that clears both wins.

The gates (``_reject_reason``):

* **error / challenge shell** — a reader that answers 200 with a page that is not the page has FAILED;
  the page is never content. Jina's own shell (``Warning: Target URL returned error <NNN>`` /
  ``Title: ERROR:``), and a SHORT text (under ``CHALLENGE_TEXT_MAX_CHARS``) carrying one of
  ``CHALLENGE_MARKERS`` (Cloudflare's "Just a moment…" / "Attention Required!", "Enable JavaScript and
  cookies", Akamai's "Access Denied", CloudFront's "Request blocked", a captcha, a bare "403 Forbidden"…).
  The detection is deliberately conservative — short texts only — so a genuine article that mentions
  "access denied" in a long body is never rejected. A bare "Cloudflare" counts only on a tiny body.
* **content floor** — fewer than ``min_content_chars`` characters of extracted text (title line excluded)
  is THIN: typically the navigation shell of a JS-rendered page (Tavily is prone to it). ``0`` disables.

Exhausted → ONE ``Error: web_fetch ladder exhausted — jina: <reason>; firecrawl: <reason>; tavily:
<reason>`` string: the model sees it and the research prompts send it to the browser tools next. Every
rung's outcome is logged at INFO with the tier and the reason — never the content, never a key (reasons
are redacted against every key the ladder knows and the logged URL is stripped of its query string).

**Model-visible contract.** The tool name, docstring and argument schema are IDENTICAL to
``deerflow.community.jina_ai.tools.web_fetch_tool`` (pinned by ``tests/test_wri_chained_fetch.py``), so
switching ``config.yaml`` from ``use: deerflow.community.jina_ai.tools:web_fetch_tool`` to
``use: deerflow.community.wri_chained_fetch.tools:web_fetch_tool`` changes nothing the model sees. The
Jina rung is the Jina tool itself (same ``JinaClient.crawl`` call — ``return_format="html"``, ``timeout``,
``proxy``, ``trust_env`` — same ``ReadabilityExtractor`` off the event loop via ``asyncio.to_thread``, same
``# <title>\n\n<markdown>`` document capped at ``max_chars``), so with ``tiers: [jina]`` a successful
fetch is byte-identical to the Jina tool's. The Firecrawl rung closes the per-call client's pooled async
HTTP client after every call (upstream fix #6013, ``5312271f``, mirrored here because it is not yet in
``local-fixes``); teardown is best-effort and never masks the rung's own result.

``config.yaml`` stanza (every key optional; defaults shown; ``$VAR`` references resolve from ``.env``)::

    tools:
      - name: web_fetch
        group: web
        use: deerflow.community.wri_chained_fetch.tools:web_fetch_tool
        timeout: 60               # seconds: Jina X-Timeout + httpx timeout; also bounds EACH fallback rung
        proxy: null               # forwarded to the Jina httpx client, exactly as the Jina tool does
        trust_env: true           # idem
        min_content_chars: 500    # content floor on the extracted text (title line excluded); 0 disables
        max_chars: 4096           # output cap — the stock tools' cap
        tiers: [jina, firecrawl, tavily]   # the ladder in order; drop a name to disable that rung
        firecrawl_api_key: null   # optional; otherwise the SDK reads FIRECRAWL_API_KEY from the environment
        firecrawl_base_url: null  # optional self-hosted Firecrawl (api_url)
        tavily_api_key: null      # optional; otherwise the SDK reads TAVILY_API_KEY from the environment

Environment (names only): ``JINA_API_KEY`` (optional — higher Jina rate limit), ``FIRECRAWL_API_KEY``
(without it, and without ``firecrawl_api_key``, the Firecrawl rung is skipped — logged), ``TAVILY_API_KEY``
(idem for the Tavily rung). Worst case wall-clock = ``timeout`` × the number of enabled rungs.

Smoke after a rebuild — inside the gateway: a known-good long page, a URL that forces the ladder (Jina got a
CloudFront 403 shell there on 25 Sep 2026 — pmi finding) and a URL every rung must fail. INFO logging prints
one ``web_fetch ladder:`` line per rung; the second URL must show ``tier=jina outcome=fail`` and then a later
rung (or the exhausted error) — never a 200 shell as content; the third must end in the exhausted error naming
all three reasons. One shell command (the ``\``-newlines are shell continuations; paste as is)::

    sg docker -c "docker exec -w /app/backend -e PYTHONPATH=/app/backend deer-flow-gateway .venv/bin/python -c \"import asyncio, logging; \
      logging.basicConfig(level=logging.INFO, force=True); from deerflow.community.wri_chained_fetch.tools import web_fetch_tool as t; \
      [print(u, '->', len(r), 'chars:', r[:120].replace(chr(10), ' ')) for u in ('https://en.wikipedia.org/wiki/Supply_chain', \
      'https://www.singstat.gov.sg/', 'https://httpbin.org/status/403') for r in (asyncio.run(t.ainvoke({'url': u})),)]\""
"""

import asyncio
import inspect
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from firecrawl import AsyncFirecrawlApp
from langchain.tools import tool
from tavily import AsyncTavilyClient

from deerflow.community.jina_ai.jina_client import JinaClient
from deerflow.config import get_app_config
from deerflow.utils.readability import ReadabilityExtractor

logger = logging.getLogger(__name__)

readability_extractor = ReadabilityExtractor()

TOOL_NAME = "web_fetch"
DEFAULT_TIERS: tuple[str, ...] = ("jina", "firecrawl", "tavily")
DEFAULT_TIMEOUT = 60
DEFAULT_MIN_CONTENT_CHARS = 500
DEFAULT_MAX_CHARS = 4096

CHALLENGE_TEXT_MAX_CHARS = 2_500
"""A text this long or longer is never taken for a challenge shell (conservative, as in ``pmi_engine.fetch``)."""
WEAK_MARKER_MAX_CHARS = 600
"""A weak marker (a bare "Cloudflare") counts only on a body this tiny."""
CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",  # Cloudflare interstitial title
    "enable javascript and cookies",  # Cloudflare / PerimeterX
    "attention required",  # Cloudflare block page title
    "checking your browser",
    "verify you are human",
    "access denied",  # Akamai
    "request blocked",  # CloudFront
    "the request could not be satisfied",  # CloudFront
    "generated by cloudfront",
    "captcha",
    "pardon our interruption",  # Distil / Imperva
    "403 forbidden",  # bare nginx / Apache status page
    "404 not found",  # a single reader's 404 is no proof the page is gone (pmi rule) — try the next rung
)
"""Case-insensitive phrases that mark a challenge / denial shell a server or CDN serves with a 200.
Consulted ONLY on a short text (under ``CHALLENGE_TEXT_MAX_CHARS``)."""
WEAK_CHALLENGE_MARKERS: tuple[str, ...] = ("cloudflare",)

REASON_MAX_CHARS = 160
EXHAUSTED_PREFIX = "Error: web_fetch ladder exhausted — "

_JINA_TARGET_ERROR_RE = re.compile(r"Warning: Target URL returned error (\d{3})\b")
_JINA_TITLE_ERROR_RE = re.compile(r"(?:^|\n)\s*Title: ERROR:")
_WS_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LadderConfig:
    timeout: int = DEFAULT_TIMEOUT
    proxy: str | None = None
    trust_env: bool = True
    min_content_chars: int = DEFAULT_MIN_CONTENT_CHARS
    max_chars: int = DEFAULT_MAX_CHARS
    tiers: tuple[str, ...] = DEFAULT_TIERS
    firecrawl_api_key: str | None = None
    firecrawl_base_url: str | None = None
    tavily_api_key: str | None = None


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_int(value: object, default: int, *, minimum: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return default
    else:
        return default
    return parsed if parsed >= minimum else default


def _coerce_str(value: object) -> str | None:
    """A non-empty stripped string, else None (an unresolved ``$VAR`` arrives as an empty string)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _coerce_tiers(value: object) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_TIERS
    raw: list[object]
    if isinstance(value, str):
        raw = [part for part in re.split(r"[,\s]+", value) if part]
    elif isinstance(value, list | tuple):
        raw = list(value)
    else:
        logger.warning("web_fetch ladder: ignoring unusable 'tiers' config of type %s; using the default ladder", type(value).__name__)
        return DEFAULT_TIERS
    tiers: list[str] = []
    for item in raw:
        name = str(item).strip().lower()
        if name not in DEFAULT_TIERS:
            logger.warning("web_fetch ladder: ignoring unknown tier %r in 'tiers' config", name)
            continue
        if name not in tiers:
            tiers.append(name)
    if not tiers:
        logger.warning("web_fetch ladder: 'tiers' config enables no known tier; using the default ladder")
        return DEFAULT_TIERS
    return tuple(tiers)


def _load_config() -> LadderConfig:
    config = get_app_config().get_tool_config(TOOL_NAME)
    extra = getattr(config, "model_extra", None) or {}
    return LadderConfig(
        timeout=_coerce_int(extra.get("timeout"), DEFAULT_TIMEOUT, minimum=1),
        proxy=_coerce_str(extra.get("proxy")),
        trust_env=_coerce_bool(extra.get("trust_env"), True),
        min_content_chars=_coerce_int(extra.get("min_content_chars"), DEFAULT_MIN_CONTENT_CHARS, minimum=0),
        max_chars=_coerce_int(extra.get("max_chars"), DEFAULT_MAX_CHARS, minimum=1),
        tiers=_coerce_tiers(extra.get("tiers")),
        firecrawl_api_key=_coerce_str(extra.get("firecrawl_api_key")),
        firecrawl_base_url=_coerce_str(extra.get("firecrawl_base_url")),
        tavily_api_key=_coerce_str(extra.get("tavily_api_key")),
    )


# ---------------------------------------------------------------------------
# gates — a 200 that is not the page is never content
# ---------------------------------------------------------------------------


def _jina_error_shell(text: str) -> str | None:
    """Jina answers 200 with a page of its own when the target refused it: ``Warning: Target URL
    returned error <NNN>`` (→ ``target returned <NNN>``) or a ``Title: ERROR:`` header (→ server error)."""
    head = text[:2_000]
    match = _JINA_TARGET_ERROR_RE.search(head)
    if match is not None and int(match.group(1)) >= 400:
        return f"jina error shell (target returned {match.group(1)})"
    if _JINA_TITLE_ERROR_RE.search(head) is not None:
        return "jina error shell (Title: ERROR)"
    return None


def _challenge_marker(text: str) -> str | None:
    """The marker a SHORT stripped text carries, or None; a long text is never a shell."""
    stripped = text.strip()
    if not stripped or len(stripped) >= CHALLENGE_TEXT_MAX_CHARS:
        return None
    lowered = stripped.lower()
    for marker in CHALLENGE_MARKERS:
        if marker in lowered:
            return marker
    if len(stripped) < WEAK_MARKER_MAX_CHARS:
        for marker in WEAK_CHALLENGE_MARKERS:
            if marker in lowered:
                return marker
    return None


def _reject_reason(title: str, body: str, min_content_chars: int) -> str | None:
    """None when ``title`` + ``body`` is content; otherwise why it is not (shell first, then the floor)."""
    shell = _jina_error_shell(f"{title}\n{body}")
    if shell is not None:
        return shell
    marker = _challenge_marker(f"{title}\n{body}")
    if marker is not None:
        return f"challenge/error shell ({marker!r})"
    length = len(body.strip())
    if length < min_content_chars:
        return f"thin content ({length} chars < min_content_chars {min_content_chars})"
    return None


# ---------------------------------------------------------------------------
# clients (module-level factories so the tests can swap them for fakes)
# ---------------------------------------------------------------------------


def _get_firecrawl_client(api_key: str | None, api_url: str | None) -> AsyncFirecrawlApp:
    """Mirror of ``deerflow.community.firecrawl.tools._get_firecrawl_client`` keyed on this tool's own
    ``firecrawl_api_key`` / ``firecrawl_base_url``; a None key lets the SDK read FIRECRAWL_API_KEY."""
    kwargs: dict[str, object] = {"api_key": api_key}
    if api_url:
        kwargs["api_url"] = api_url
    return AsyncFirecrawlApp(**kwargs)  # type: ignore[arg-type]


def _get_tavily_client(api_key: str | None) -> AsyncTavilyClient:
    return AsyncTavilyClient(api_key=api_key)


async def _aclose_firecrawl_client(client: object) -> None:
    """Best-effort close of the pooled async HTTP client a per-call app constructed (upstream #6013).

    ``AsyncFirecrawlApp`` eagerly builds an ``httpx.AsyncClient``-backed pool in its constructor and
    exposes no public teardown, so reach it through the delegating v2 client. Older SDKs in the declared
    range lack that attribute, and teardown runs from a ``finally`` — absence or failure here must never
    mask the rung's own result.
    """
    pooled = getattr(getattr(client, "_v2_client", None), "async_http_client", None)
    if pooled is None:
        return
    try:
        close = getattr(pooled, "close", None)
        if not callable(close):
            logger.warning("Firecrawl async HTTP pool has no close method")
            return
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.warning("Failed to close the Firecrawl async HTTP pool", exc_info=True)


async def _aclose_tavily_client(client: object) -> None:
    """Best-effort ``await client.close()`` — never masks the rung's own result."""
    close = getattr(client, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.warning("Failed to close the Tavily client", exc_info=True)


# ---------------------------------------------------------------------------
# rungs — each returns (document, reason): document is "# <title>\n\n<body>" or None
# ---------------------------------------------------------------------------

RungResult = tuple[str | None, str]


def _document(title: str, body: str) -> str:
    return f"# {title}\n\n{body}"


async def _run_jina(url: str, config: LadderConfig) -> RungResult:
    body = await JinaClient().crawl(url, return_format="html", timeout=config.timeout, proxy=config.proxy, trust_env=config.trust_env)
    if not isinstance(body, str) or not body.strip():
        return None, "empty response"
    if body.startswith("Error:"):
        return None, body.removeprefix("Error:").strip()
    shell = _jina_error_shell(body)
    if shell is not None:
        return None, shell
    article = await asyncio.to_thread(readability_extractor.extract_article, body, url=url)
    title = article.title
    content = article.to_markdown(including_title=False)
    reason = _reject_reason(title, content, config.min_content_chars)
    if reason is not None:
        return None, reason
    return _document(title, content), f"ok ({len(content)} chars)"


async def _run_firecrawl(url: str, config: LadderConfig) -> RungResult:
    api_key = config.firecrawl_api_key or _coerce_str(os.environ.get("FIRECRAWL_API_KEY"))
    if api_key is None:
        return None, "skipped (FIRECRAWL_API_KEY not set and no firecrawl_api_key configured)"
    client = _get_firecrawl_client(api_key=api_key, api_url=config.firecrawl_base_url)
    try:
        result = await asyncio.wait_for(client.scrape(url, formats=["markdown"]), timeout=config.timeout)
    finally:
        await _aclose_firecrawl_client(client)
    content = getattr(result, "markdown", None) or ""
    metadata = getattr(result, "metadata", None)
    title = _coerce_str(getattr(metadata, "title", None)) or "Untitled"
    if not content.strip():
        return None, "no content found"
    reason = _reject_reason(title, content, config.min_content_chars)
    if reason is not None:
        return None, reason
    return _document(title, content), f"ok ({len(content)} chars)"


async def _run_tavily(url: str, config: LadderConfig) -> RungResult:
    api_key = config.tavily_api_key or _coerce_str(os.environ.get("TAVILY_API_KEY"))
    if api_key is None:
        return None, "skipped (TAVILY_API_KEY not set and no tavily_api_key configured)"
    client = _get_tavily_client(api_key=api_key)
    try:
        response = await asyncio.wait_for(client.extract([url]), timeout=config.timeout)
    finally:
        await _aclose_tavily_client(client)
    if not isinstance(response, dict):
        return None, "unexpected extract response"
    failed = response.get("failed_results") or []
    if failed:
        first_failure = failed[0] if isinstance(failed[0], dict) else {}
        return None, f"extract failed: {first_failure.get('error') or 'unknown error'}"
    results = response.get("results") or []
    if not results or not isinstance(results[0], dict):
        return None, "no results found"
    result = results[0]
    # Extract results guarantee a URL and content, but not a page title.
    title = _coerce_str(result.get("title")) or _coerce_str(result.get("url")) or url
    content = result.get("raw_content") or ""
    if not isinstance(content, str) or not content.strip():
        return None, "no content found"
    reason = _reject_reason(title, content, config.min_content_chars)
    if reason is not None:
        return None, reason
    return _document(title, content), f"ok ({len(content)} chars)"


_RUNGS: dict[str, Callable[[str, LadderConfig], Awaitable[RungResult]]] = {
    "jina": _run_jina,
    "firecrawl": _run_firecrawl,
    "tavily": _run_tavily,
}


# ---------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------


def _known_secrets(config: LadderConfig) -> tuple[str, ...]:
    candidates = (
        config.firecrawl_api_key,
        config.tavily_api_key,
        os.environ.get("FIRECRAWL_API_KEY"),
        os.environ.get("TAVILY_API_KEY"),
        os.environ.get("JINA_API_KEY"),
    )
    return tuple(secret for secret in candidates if isinstance(secret, str) and len(secret.strip()) >= 8)


def _short_reason(reason: str, secrets: tuple[str, ...]) -> str:
    text = _WS_RE.sub(" ", reason).strip() or "unknown"
    for secret in secrets:
        text = text.replace(secret, "***")
    if len(text) > REASON_MAX_CHARS:
        text = text[: REASON_MAX_CHARS - 1] + "…"
    return text


def _loggable_url(url: str) -> str:
    """The URL without its query string, fragment or userinfo (a signed query or ``user:pass@`` is a credential)."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
    except ValueError:
        return url[:120]


async def _run_ladder(url: str, config: LadderConfig) -> str:
    secrets = _known_secrets(config)
    shown_url = _loggable_url(url)
    outcomes: list[tuple[str, str]] = []
    for tier in config.tiers:
        rung = _RUNGS[tier]
        try:
            document, reason = await rung(url, config)
        except TimeoutError:
            document, reason = None, f"timed out after {config.timeout}s"
        except Exception as exc:
            document, reason = None, f"{type(exc).__name__}: {exc}"
        reason = _short_reason(reason, secrets)
        if document is not None:
            logger.info("web_fetch ladder: tier=%s outcome=ok reason=%s url=%s", tier, reason, shown_url)
            return document[: config.max_chars]
        logger.info("web_fetch ladder: tier=%s outcome=fail reason=%s url=%s", tier, reason, shown_url)
        outcomes.append((tier, reason))
    for tier in DEFAULT_TIERS:
        if tier not in config.tiers:
            outcomes.append((tier, "disabled"))
    summary = "; ".join(f"{tier}: {reason}" for tier, reason in outcomes)
    logger.warning("web_fetch ladder exhausted — %s url=%s", summary, shown_url)
    return EXHAUSTED_PREFIX + summary


@tool("web_fetch", parse_docstring=True)
async def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL.
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    return await _run_ladder(url, _load_config())
