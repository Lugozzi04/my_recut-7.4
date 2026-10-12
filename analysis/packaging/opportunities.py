"""Creative Opportunity Matrix: evidence-backed editorial hooks, before any concept is designed.

Each CreativeOpportunity is a HYPOTHESIS computed locally from signals that exist (highlight
signals, transcript words, game markers, available assets). It names candidate story types,
the evidence ids that support them, which assets could carry the story and what is missing.
The agent confirms, rejects or reframes them; it never has to invent a story to fill a slot.

Ranking is deliberately not audio-led: a strategic explanation or a decision weighs as much as
a loud reaction; diversity across games and story types is preferred in the shortlist.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from .assets import VisualAssetManifest
from .models import GameContext, HighlightCandidate, TranscriptLine

OPPORTUNITY_VERSION = "creative_opportunities_v1"
STORY_TYPES = ("surprising_result", "decisive_card", "impressive_board", "strategic_decision", "unexpected_reaction",
               "matchup_story", "comeback", "unusual_interaction", "educational_explanation")
TREATMENTS = ("reaction_cutout", "card_hero", "board_zoom", "split_moments", "cinematic_moment", "face_and_card",
              "text_led")
DECISION_WORDS = {"tengo", "scelgo", "scegli", "conviene", "devo", "meglio", "giusto", "sbagliato", "mulligan",
                  "cambio", "pesco", "keep", "choose", "should", "decide", "opzione", "opzioni", "alternativa", "oppure"}
EXPLAIN_WORDS = {"perché", "perche", "quindi", "infatti", "lista", "mazzo", "deck", "matchup", "contro", "piano",
                 "strategia", "because", "plan", "combo", "sinergia", "valore"}
REVERSAL_WORDS = {"rimonta", "recuperabile", "girata", "ribaltata", "comeback", "pensavo", "persa", "salvato", "letale",
                  "lethal", "incredibile", "assurdo"}
_TOKEN = re.compile(r"[\wàèéìòóù']+", re.UNICODE)


@dataclass
class CreativeOpportunity:
    opportunity_id: str
    game_id: str | None
    highlight_ids: list[str]
    narrative_hook: str
    story_type: str
    story_type_alternatives: list[str]
    evidence_ids: list[str]
    candidate_primary_subjects: list[str]
    candidate_secondary_subjects: list[str]
    available_asset_ids: list[str]
    visual_strengths: list[str]
    visual_limitations: list[str]
    possible_treatments: list[str]
    weight: float = 0.0
    signals: list[str] = field(default_factory=list)

    def to_mapping(self) -> dict[str, Any]:
        value = asdict(self)
        value["weight"] = round(self.weight, 3)
        return value


def _words(lines: Sequence[TranscriptLine]) -> set[str]:
    return {t.lower() for line in lines for t in _TOKEN.findall(line.text)}


def _story_types(h: HighlightCandidate, words: set[str], game: GameContext | None, has_card: bool,
                 has_face: bool) -> list[tuple[str, float, str]]:
    """(story_type, strength, reason) candidates supported by the highlight's own signals."""
    out: list[tuple[str, float, str]] = []
    sources = set(h.evidence_sources)
    if words & DECISION_WORDS:
        out.append(("strategic_decision", 0.9, "decision words: " + ", ".join(sorted(words & DECISION_WORDS)[:4])))
    if "explanation" in sources or len(words & EXPLAIN_WORDS) >= 2:
        out.append(("educational_explanation", 0.75, "continuous explanation / deck & matchup words"))
    if "near_result" in sources and game is not None and game.result == "WIN":
        out.append(("decisive_card" if has_card else "surprising_result", 0.85, "last moments before the WIN"))
    if words & REVERSAL_WORDS:
        out.append(("comeback", 0.7, "reversal words: " + ", ".join(sorted(words & REVERSAL_WORDS)[:4])))
    if ("reaction" in sources or "audio_peak" in sources) and has_face:
        out.append(("unexpected_reaction", 0.7 if "reaction" in sources else 0.55, "spoken reaction / energy peak"))
    if "visual_change" in sources and float(h.visual.get("keyframe_change_max", 0)) > 0.5:
        out.append(("unusual_interaction", 0.5, "strong on-screen change"))
    if "game_intro" in sources:
        out.append(("matchup_story", 0.6, "start of the game: matchup / plan"))
    if not out:
        out.append(("impressive_board", 0.4, "visual moment without a stronger signal"))
    return sorted(out, key=lambda item: item[1], reverse=True)


def build_opportunities(highlights: Sequence[HighlightCandidate], games: Sequence[GameContext],
                        lines: Sequence[TranscriptLine], manifest: VisualAssetManifest, *,
                        limit: int = 10) -> list[CreativeOpportunity]:
    by_line = {line.id: line for line in lines}
    game_by_id = {g.game_id: g for g in games}
    out: list[CreativeOpportunity] = []
    for h in highlights:
        context = [by_line[i] for i in h.transcript_ids if i in by_line]
        words = _words(context)
        assets = manifest.for_highlight(h.id)
        subjects = [a for a in assets if a.type == "webcam_subject"]
        isolated = [a.id for a in sorted(subjects, key=lambda a: -a.quality.get("face_score", 0.0))
                    if a.quality.get("isolated")]
        frames = [a.id for a in assets if a.type == "gameplay_frame"]
        regions = [a.id for a in sorted((a for a in assets if a.type == "board_region"),
                                        key=lambda a: -a.quality.get("salience", 0.0))]
        cards = [a.id for a in assets if a.type == "card_crop" and "primary" in a.uses]
        game = game_by_id.get(h.game_id or "")
        types = _story_types(h, words, game, bool(cards), bool(isolated))
        primary = [*cards[:1], *regions[:1], *isolated[:1], *frames[:1]]
        secondary = [*isolated[1:2], *regions[1:2], *frames[1:3]]
        treatments = []
        if isolated:
            treatments += ["reaction_cutout", "face_and_card"] if cards else ["reaction_cutout"]
        if cards:
            treatments.append("card_hero")
        if regions:
            treatments.append("board_zoom")
        if len(frames) >= 2:
            treatments.append("split_moments")
        treatments += ["cinematic_moment", "text_led"]
        strengths, limitations = [], []
        if isolated:
            strengths.append(f"clean creator cut-out ({isolated[0]})")
        elif subjects:
            limitations.append("webcam visible but not isolated: only small or omitted")
        else:
            limitations.append("no webcam for this moment")
        if regions:
            strengths.append(f"readable board region ({regions[0]})")
        if cards:
            strengths.append("card read in the video")
        weak = [a.id for a in assets if a.type == "gameplay_frame" and a.quality.get("sharpness", 1e9) < 300]
        if weak:
            limitations.append(f"soft frames: {', '.join(weak[:3])}")
        evidence = list(h.transcript_ids[:6]) + frames[:2]
        hook = types[0][2]
        weight = types[0][1] + 0.15 * min(1.0, h.local_score / 2.0) + 0.1 * len(types[1:3])
        out.append(CreativeOpportunity(
            f"O{len(out) + 1:02d}", h.game_id, [h.id], hook, types[0][0], [t for t, _, _ in types[1:4]], evidence,
            primary, secondary, [a.id for a in assets], strengths, limitations, list(dict.fromkeys(treatments)),
            weight, list(h.evidence_sources)))
    for game in games:  # one matchup/arc opportunity per real game
        if game.index is None or not game.highlight_ids:
            continue
        frames = [a.id for a in manifest.assets if a.game_id == game.game_id and a.type in ("gameplay_frame",
                                                                                          "background")][:4]
        out.append(CreativeOpportunity(
            f"O{len(out) + 1:02d}", game.game_id, list(game.highlight_ids), f"the arc of game {game.index} "
            f"({game.result or 'no result'})", "matchup_story",
            ["comeback", "surprising_result"], list(game.transcript_ids[:4]) + frames[:2], frames[:2], frames[2:4],
            frames, ["whole-game arc"], [], ["split_moments", "cinematic_moment", "text_led"], 0.5, ["game_arc"]))
    return shortlist(out, limit=limit)


def shortlist(opportunities: Sequence[CreativeOpportunity], *, limit: int) -> list[CreativeOpportunity]:
    """Greedy diverse selection: weight, then penalties for repeating a story type or a game."""
    chosen: list[CreativeOpportunity] = []
    pool = list(opportunities)
    while pool and len(chosen) < limit:
        def gain(o: CreativeOpportunity) -> float:
            same_type = sum(1 for c in chosen if c.story_type == o.story_type)
            same_game = sum(1 for c in chosen if c.game_id == o.game_id)
            return o.weight - 0.35 * same_type - 0.1 * same_game
        best = max(pool, key=gain)
        chosen.append(best)
        pool.remove(best)
    for n, o in enumerate(chosen, start=1):
        o.opportunity_id = f"O{n:02d}"
    return chosen


def opportunity_lines(opportunities: Sequence[CreativeOpportunity]) -> list[str]:
    lines = []
    for o in opportunities:
        lines.append(f"- {o.opportunity_id} {o.story_type} (alt: {', '.join(o.story_type_alternatives) or '-'}) "
                     f"game={o.game_id} highlights={','.join(o.highlight_ids)} | hook: {o.narrative_hook} | "
                     f"primary candidates {','.join(o.candidate_primary_subjects) or '-'} | secondary "
                     f"{','.join(o.candidate_secondary_subjects) or '-'} | treatments {','.join(o.possible_treatments)} | "
                     f"strengths: {'; '.join(o.visual_strengths) or '-'} | limits: {'; '.join(o.visual_limitations) or '-'}")
    return lines


def from_mappings(raw: Sequence[Mapping[str, Any]]) -> list[CreativeOpportunity]:
    names = CreativeOpportunity.__dataclass_fields__.keys()
    return [CreativeOpportunity(**{k: item[k] for k in names if k in item}) for item in raw]
