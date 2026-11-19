"""Create a campaign-scoped, nonbinding cabinet brief from a player message."""
from language_detection import detect_language, language_name
from voices_history.domestic_schema import TOPICS


def classify(provider, text, observation):
    language = detect_language(text)
    classifier = getattr(provider, "classify_domestic_proposals", None)
    raw = classifier(text, language_name(language)) if classifier else None
    proposals = raw.get("proposals") if isinstance(raw, dict) and set(raw) == {"proposals"} else None
    draft = {"campaign_id": observation["campaign"], "country": observation["player"],
             "game_day": observation["day"], "language": language, "source_text": text,
             "status": "analysis_unavailable", "proposals": [], "game_effect_applied": False}
    if not isinstance(proposals, list) or len(proposals) > 3:
        return draft
    accepted = []
    for item in proposals:
        if not isinstance(item, dict) or set(item) != {"topic", "excerpt"}:
            return draft
        topic, excerpt = item["topic"], item["excerpt"]
        if (topic not in TOPICS or not isinstance(excerpt, str) or not 3 <= len(excerpt) <= 600
                or not excerpt.strip() or excerpt not in text):
            return draft
        if item not in accepted:
            accepted.append(item)
    draft.update(status="draft" if accepted else "discussion", proposals=accepted)
    return draft
