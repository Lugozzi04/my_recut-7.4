"""Official card images for thumbnails: HearthstoneJSON renders, in the audience's language.

The creator's best cover used the clean official card ("Lama dell'Impero Nero", Italian) instead of a blurry crop
of a stream frame. This provider finds a card by name (any of the indexed locales: names read in an English tier
list website or said in Italian both match) among collectible cards and downloads its full card render in the
target locale. Everything is cached on disk; no network means no official image (the video crop stays).

Card images are (c) Blizzard Entertainment; HearthstoneJSON (hearthstonejson.com) is an unofficial fan project
that serves them. The provenance of every image records both, so the creator can judge their use.
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
import urllib.request
from pathlib import Path
from typing import Any, Callable

API = "https://api.hearthstonejson.com/v1/latest/{locale}/cards.collectible.json"
RENDER = "https://art.hearthstonejson.com/v1/render/latest/{locale}/512x/{card_id}.png"
LOCALES = {"it": "itIT", "en": "enUS", "de": "deDE", "es": "esES", "fr": "frFR", "pt": "ptBR", "pl": "plPL",
           "ru": "ruRU", "ja": "jaJP", "ko": "koKR", "zh": "zhCN"}
NAME_LOCALES = ("enUS", "itIT")  # names are matched in English and Italian (+ the target locale)
MIN_RATIO = 0.88
LICENCE = ("Card image (c) Blizzard Entertainment, served by HearthstoneJSON (unofficial fan project); "
           "fan content use is the creator's responsibility.")

Fetch = Callable[[str], bytes]


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ReCut/thumbnail-card-art"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed https endpoints
        return response.read()


def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


class HearthstoneJsonProvider:
    """CardAssetProvider: lookup(name) -> (png path, provenance) or None."""

    def __init__(self, cache_dir: Path, *, language: str = "it", fetch: Fetch | None = None) -> None:
        self.cache_dir = Path(cache_dir)
        self.locale = LOCALES.get(language.split("-")[0].lower(), "enUS")
        self.fetch = fetch or _fetch
        self._index: dict[str, dict[str, Any]] | None = None
        self.errors: list[str] = []

    def _cards(self, locale: str) -> list[dict[str, Any]]:
        path = self.cache_dir / f"cards_{locale}.json"
        if not path.is_file():
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            data = self.fetch(API.format(locale=locale))
            json.loads(data)  # never cache a broken download
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return list(json.loads(path.read_text(encoding="utf-8")))

    def index(self) -> dict[str, dict[str, Any]]:
        """normalized name (in any indexed locale) -> {id, names{locale: name}} (every locale of that card)."""
        if self._index is None:
            names: dict[str, dict[str, str]] = {}  # card id -> {locale: name}
            keys: dict[str, str] = {}  # normalized name (any locale) -> card id
            for locale in dict.fromkeys((*NAME_LOCALES, self.locale)):
                for card in self._cards(locale):
                    if card.get("name") and card.get("id"):
                        names.setdefault(card["id"], {})[locale] = card["name"]
                        keys.setdefault(normalize(card["name"]), card["id"])
            self._index = {key: {"id": card_id, "names": names[card_id]} for key, card_id in keys.items()}
        return self._index

    def match(self, name: str) -> dict[str, Any] | None:
        key = normalize(name)
        if not key:
            return None
        try:
            index = self.index()
        except Exception as exc:  # noqa: BLE001 - offline / API change: no official image
            self.errors.append(f"card database unavailable ({exc})")
            return None
        if key in index:
            return {**index[key], "ratio": 1.0}
        close = difflib.get_close_matches(key, list(index), n=2, cutoff=MIN_RATIO)
        if not close:
            return None
        ratio = difflib.SequenceMatcher(None, key, close[0]).ratio()
        if len(close) > 1 and difflib.SequenceMatcher(None, key, close[1]).ratio() > ratio - 0.02 and \
                index[close[1]]["id"] != index[close[0]]["id"]:
            return None  # two different cards are equally close: never guess
        return {**index[close[0]], "ratio": round(ratio, 3)}

    def lookup(self, name: str) -> tuple[str, dict[str, Any]] | None:
        found = self.match(name)
        if found is None:
            return None
        path = self.cache_dir / "renders" / f"{self.locale}_{found['id']}.png"
        if not path.is_file():
            try:
                data = self.fetch(RENDER.format(locale=self.locale, card_id=found["id"]))
            except Exception as exc:  # noqa: BLE001 - optional
                self.errors.append(f"card image {found['id']} unavailable ({exc})")
                return None
            if not data.startswith(b"\x89PNG"):
                self.errors.append(f"card image {found['id']}: not a PNG")
                return None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return str(path), {"source": "hearthstonejson_render", "card_id": found["id"], "locale": self.locale,
                           "name": found["names"].get(self.locale) or next(iter(found["names"].values())),
                           "matched": name, "match_ratio": found["ratio"], "licence": LICENCE}
