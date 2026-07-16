from __future__ import annotations

import unicodedata


LEAGUE_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "Spanish La Liga": ("la liga", "laliga", "primera division"),
    "French Ligue 1": ("ligue 1", "ligue-1", "championnat de france"),
    "German Bundesliga": ("bundesliga", "1 bundesliga"),
    "Italian Serie A": ("serie a",),
    "English Premier League": ("premier league", "epl"),
    "FIFA World Cup": ("world cup", "fifa world cup", "coupe du monde"),
}

TEAM_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "inter milan": ("inter", "internazionale"),
    "ac milan": ("milan",),
    "borussia monchengladbach": ("monchengladbach", "borussia m gladbach"),
    "atletico madrid": ("atletico madrid",),
    "deportivo alaves": ("alaves",),
}

GENERIC_NAME_TOKENS = {
    "ac",
    "afc",
    "athletic",
    "atletico",
    "bk",
    "cf",
    "club",
    "fc",
    "fk",
    "football",
    "sv",
}


def compare_team_names(left: str, right: str) -> int:
    left_aliases = alias_forms(left)
    right_aliases = alias_forms(right)
    if left_aliases & right_aliases:
        return 3

    left_tokens = significant_tokens(left)
    right_tokens = significant_tokens(right)
    if left_tokens and right_tokens and left_tokens == right_tokens:
        return 2
    if left_tokens and right_tokens and left_tokens.issubset(right_tokens):
        return 1
    if left_tokens and right_tokens and right_tokens.issubset(left_tokens):
        return 1
    return 0


def alias_forms(team_name: str) -> set[str]:
    normalized = canonical_label(team_name)
    aliases = {normalized}
    for key, values in TEAM_NAME_ALIASES.items():
        known = {canonical_label(key), *(canonical_label(value) for value in values)}
        if normalized in known:
            aliases |= known
    return aliases


def significant_tokens(text: str) -> set[str]:
    return {
        token
        for token in canonical_label(text).split()
        if token and token not in GENERIC_NAME_TOKENS
    }


def canonical_label(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = "".join(char if char.isalnum() else " " for char in ascii_text.lower())
    return " ".join(cleaned.split())


def slugify(value: str) -> str:
    return canonical_label(value).replace(" ", "-")
