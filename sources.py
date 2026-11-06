"""Pluggable audio sources for non-YouTube links (podcasts, etc.).

A source turns a URL into a `Resolved` episode: a direct audio URL plus
metadata. To add a new source type, write a class with `matches(url)` and
`resolve(url)`, and decorate it with `@register`. See TODO.md for planned ones.

transcribe.py asks `resolve(url)`; if it returns None, the URL is treated as
a YouTube link (handled by yt-dlp).
"""
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

USER_AGENT = "youtube-transcripts/1.0 (+personal use)"


@dataclass
class Resolved:
    id: str
    title: str
    show: str
    audio_url: str
    page_url: str
    date: str = ""  # ISO date, if known


class SourceError(Exception):
    """A recognised link that couldn't be resolved to downloadable audio."""


_SOURCES = []


def register(cls):
    _SOURCES.append(cls())
    return cls


def resolve(url):
    """Return Resolved for the first matching source, or None if no source claims the URL."""
    for src in _SOURCES:
        if src.matches(url):
            return src.resolve(url)
    return None


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


@register
class ApplePodcasts:
    """podcasts.apple.com/<cc>/podcast/<slug>/id<SHOW>[?i=<EPISODE>].

    Uses Apple's public iTunes lookup API. With ?i= it picks that episode;
    without it, the latest episode of the show.
    """

    _RE = re.compile(r"^https?://podcasts\.apple\.com/.*?/id(\d+)")

    def matches(self, url):
        return bool(self._RE.match(url))

    def resolve(self, url):
        show_id = self._RE.match(url).group(1)
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        episode_id = (qs.get("i") or [None])[0]

        # Episodes come newest-first; 200 is the API's max per request.
        data = _get_json(
            f"https://itunes.apple.com/lookup?id={show_id}&entity=podcastEpisode&limit=200"
        )
        results = data.get("results", [])
        show = next((r for r in results if r.get("kind") == "podcast"), None)
        episodes = [r for r in results if r.get("kind") == "podcast-episode"]
        if not show and not episodes:
            raise SourceError(f"Apple Podcasts has no show with id {show_id}")

        if episode_id:
            ep = next((e for e in episodes if str(e.get("trackId")) == episode_id), None)
            if ep is None:
                # Older than the 200 most recent: look the episode up directly.
                direct = _get_json(f"https://itunes.apple.com/lookup?id={episode_id}")
                ep = next(
                    (r for r in direct.get("results", []) if r.get("kind") == "podcast-episode"),
                    None,
                )
            if ep is None:
                raise SourceError(f"Episode {episode_id} not found for show {show_id}")
        else:
            if not episodes:
                raise SourceError("Show has no episodes in Apple's listing")
            ep = episodes[0]

        audio = ep.get("episodeUrl")
        if not audio:
            raise SourceError("Apple lists no audio URL for this episode")
        return Resolved(
            id=str(ep["trackId"]),
            title=ep.get("trackName", "episode"),
            show=ep.get("collectionName") or (show or {}).get("collectionName", "?"),
            audio_url=audio,
            page_url=ep.get("trackViewUrl") or url,
            date=(ep.get("releaseDate") or "")[:10],
        )
