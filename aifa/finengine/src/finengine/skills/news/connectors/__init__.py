"""Source connectors — each pulls items from a specific source type
and emits normalized `NewsItem` instances. All connectors share the
`Connector` protocol so the pipeline can iterate them uniformly."""
from finengine.skills.news.connectors.base import Connector
from finengine.skills.news.connectors.edgar import EDGARConnector
from finengine.skills.news.connectors.edisclosure import EDisclosureConnector
from finengine.skills.news.connectors.html import HTMLConnector
from finengine.skills.news.connectors.playwright_rss import PlaywrightRSSConnector
from finengine.skills.news.connectors.rss import RSSConnector
from finengine.skills.news.connectors.telegram import TelegramConnector

__all__ = [
    "Connector", "RSSConnector", "TelegramConnector", "EDGARConnector",
    "HTMLConnector", "EDisclosureConnector", "PlaywrightRSSConnector",
    "registry",
]


def registry() -> dict[str, type[Connector]]:
    """Map connector slug (from sources.yaml `connector:` field) → class.
    Adding new connectors = adding to this dict."""
    return {
        "rss": RSSConnector,
        "telegram": TelegramConnector,
        "edgar": EDGARConnector,
        "html": HTMLConnector,
        # JS-rendered sources — both backed by `playwright_base`. Used
        # by e-disclosure.ru (RU material facts) and Cloudflare-fronted
        # RSS feeds (Business Wire, GlobeNewswire).
        "edisclosure": EDisclosureConnector,
        "playwright_rss": PlaywrightRSSConnector,
        # tavily_fallback — later.
    }
