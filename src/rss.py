from __future__ import annotations

from collections.abc import Sequence
from email.utils import format_datetime

from lxml import etree

from models import Episode, FeedConfig

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"


class RssRenderer:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def render(
        self,
        feed: FeedConfig,
        episodes: Sequence[Episode],
        feed_image: str,
    ) -> bytes:
        rss = etree.Element(
            "rss",
            version="2.0",
            nsmap={"itunes": ITUNES_NS, "atom": ATOM_NS},
        )
        channel = etree.SubElement(rss, "channel")
        self._text(channel, "title", feed.title)
        self._text(channel, "link", self.base_url)
        self._text(channel, "description", feed.description)
        self._text(channel, "language", feed.language)
        self._text(channel, f"{{{ITUNES_NS}}}author", feed.author)
        self._text(channel, f"{{{ITUNES_NS}}}explicit", "false")
        etree.SubElement(
            channel,
            f"{{{ATOM_NS}}}link",
            href=f"{self.base_url}/feeds/{feed.slug}.xml",
            rel="self",
            type="application/rss+xml",
        )
        if feed_image:
            etree.SubElement(channel, f"{{{ITUNES_NS}}}image", href=feed_image)
            image = etree.SubElement(channel, "image")
            self._text(image, "url", feed_image)
            self._text(image, "title", feed.title)
            self._text(image, "link", self.base_url)

        for episode in episodes:
            item = etree.SubElement(channel, "item")
            self._text(item, "title", episode.title)
            guid = self._text(item, "guid", episode.guid)
            guid.set("isPermaLink", "false")
            self._text(item, "pubDate", format_datetime(episode.published_at))
            self._text(item, "description", episode.description or episode.title)
            self._text(item, f"{{{ITUNES_NS}}}duration", str(episode.duration))
            self._text(item, f"{{{ITUNES_NS}}}explicit", "false")
            if episode.image_url:
                etree.SubElement(
                    item,
                    f"{{{ITUNES_NS}}}image",
                    href=episode.image_url,
                )
            etree.SubElement(
                item,
                "enclosure",
                url=episode.media_url,
                length=str(episode.media_length),
                type=episode.media_type,
            )

        return etree.tostring(
            rss,
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=True,
        )

    @staticmethod
    def _text(parent: etree._Element, tag: str, value: str) -> etree._Element:
        element = etree.SubElement(parent, tag)
        element.text = value
        return element
