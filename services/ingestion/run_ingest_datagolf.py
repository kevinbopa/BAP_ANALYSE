"""Ingestion Golf V2 depuis DataGolf.

    py services/ingestion/run_ingest_datagolf.py

Couvre le circuit masculin complet (tours configures dans DATAGOLF_TOURS).
Pour chaque tour ayant un evenement actif :
  * tournoi + joueurs (identite dg stable, ranks, pays)
  * probabilites du modele DataGolf (win/top5/10/20/make_cut) -> predictions
  * cotes books outrights (dont bet365) -> core.golf_odds
  * cotes + fair maison des matchups (2-balls + 3-balls) -> matchup odds/preds

Le modele DataGolf (strokes-gained + course-fit) remplace le consensus V1 :
le classement n'est plus un miroir des cotes mais une vraie proba par joueur.
"""
from __future__ import annotations

import argparse
from datetime import UTC, date, datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str((Path(__file__).resolve().parent / "src").resolve()))

from spe_ingestion.clients.datagolf import DataGolfClient, DataGolfError
from spe_ingestion.config import DataGolfSettings
from spe_ingestion.db import DatabaseSettings, connect_db


# DataGolf -> nos codes marche (outrights).
OUTRIGHT_MARKETS = {
    "win": "TOURNAMENT_WINNER",
    "top_5": "TOP_5",
    "top_10": "TOP_10",
    "top_20": "TOP_20",
    "make_cut": "MAKE_CUT",
}
# DataGolf -> nos codes marche (matchups).
MATCHUP_MARKETS = {
    "tournament_matchups": "TOURNAMENT_MATCHUP",
    "round_matchups": "ROUND_MATCHUP",
    "3_balls": "THREE_BALL",
}
# Le fair-odd du modele DataGolf est stocke comme un "book" synthetique : le
# runner prediction (role spe_app_rw) le lira pour produire model.golf_*.
# `baseline_history_fit` = modele avec course-fit (le meilleur).
DATAGOLF_BOOK = "DATAGOLF"
DATAGOLF_MODEL_FIELD = "baseline_history_fit"


def _display_name(raw: str) -> str:
    """DataGolf donne "Nom, Prenom" -> on affiche "Prenom Nom"."""
    raw = (raw or "").strip()
    if "," in raw:
        last, first = raw.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return raw


def _season_from(field: dict) -> int:
    for key in ("date_start", "date_end"):
        value = str(field.get(key) or "")
        if len(value) >= 4 and value[:4].isdigit():
            return int(value[:4])
    return datetime.now(UTC).year


def _parse_date(value: object) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _valid_date_arg(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        raise argparse.ArgumentTypeError(f"Date invalide: {value}")


def _event_value(event: dict, *keys: str) -> object:
    for key in keys:
        value = event.get(key)
        if value not in (None, ""):
            return value
    return None


def _schedule_events(payload: dict, requested_tour: str) -> list[dict]:
    """Normalise les formats schedule possibles sans supposer une seule forme."""
    candidates: list[dict] = []
    for key in ("schedule", "events", "tournaments"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend([item for item in value if isinstance(item, dict)])
    if not candidates:
        for key, value in payload.items():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        enriched = dict(item)
                        enriched.setdefault("tour", key)
                        candidates.append(enriched)
    for item in candidates:
        item.setdefault("tour", requested_tour)
    return candidates


def _catalog_provider_id(tour: str, event: dict) -> str:
    event_id = _event_value(event, "event_id", "dg_event_id", "tournament_id")
    season = _event_value(event, "season") or _season_from(event)
    if event_id is not None:
        return f"{tour}-{season}-{event_id}"
    name = str(_event_value(event, "event_name", "tournament_name", "name") or "event").strip()
    start = str(_event_value(event, "date_start", "start_date", "event_date", "date") or "")
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in f"{tour}-{season}-{start}-{name}")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:180]


def _upsert_catalog_tournament(cursor, tour: str, event: dict) -> int:
    tour = str(_event_value(event, "tour", "tour_code") or tour).strip().lower() or tour
    event_id = _event_value(event, "event_id", "dg_event_id", "tournament_id")
    season = int(_event_value(event, "season") or _season_from(event))
    provider_event_id = _catalog_provider_id(tour, event)
    name = str(_event_value(event, "event_name", "tournament_name", "name") or provider_event_id).strip()
    date_start = _parse_date(_event_value(event, "date_start", "start_date", "event_date", "date"))
    date_end = _parse_date(_event_value(event, "date_end", "end_date"))
    commence = datetime.combine(date_start, datetime.min.time(), tzinfo=UTC) if date_start else None
    cursor.execute(
        """
        INSERT INTO core.golf_tournaments (
            provider_event_id, sport_key, tournament_name, sport_title,
            commence_time, dg_event_id, tour_code, course_name,
            date_start, date_end, current_round, season, catalog_status,
            last_catalog_sync_at, raw_event_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                'DISCOVERED', now(), %s::jsonb)
        ON CONFLICT (provider_event_id) DO UPDATE
        SET tournament_name = EXCLUDED.tournament_name,
            sport_title = COALESCE(EXCLUDED.sport_title, core.golf_tournaments.sport_title),
            commence_time = COALESCE(EXCLUDED.commence_time, core.golf_tournaments.commence_time),
            dg_event_id = COALESCE(EXCLUDED.dg_event_id, core.golf_tournaments.dg_event_id),
            tour_code = COALESCE(EXCLUDED.tour_code, core.golf_tournaments.tour_code),
            course_name = COALESCE(EXCLUDED.course_name, core.golf_tournaments.course_name),
            date_start = COALESCE(EXCLUDED.date_start, core.golf_tournaments.date_start),
            date_end = COALESCE(EXCLUDED.date_end, core.golf_tournaments.date_end),
            current_round = COALESCE(EXCLUDED.current_round, core.golf_tournaments.current_round),
            season = COALESCE(EXCLUDED.season, core.golf_tournaments.season),
            catalog_status = CASE
                WHEN core.golf_tournaments.catalog_status IN ('FIELD_SYNCED', 'ODDS_SYNCED', 'COMPLETED')
                THEN core.golf_tournaments.catalog_status
                ELSE 'DISCOVERED'
            END,
            last_catalog_sync_at = now(),
            raw_event_json = core.golf_tournaments.raw_event_json || EXCLUDED.raw_event_json,
            updated_at = now()
        RETURNING golf_tournament_id
        """,
        (
            provider_event_id,
            f"datagolf_{tour}",
            name,
            tour.upper(),
            commence,
            int(event_id) if str(event_id or "").isdigit() else None,
            tour,
            str(_event_value(event, "course_name", "course") or "").strip() or None,
            date_start,
            date_end,
            event.get("current_round"),
            season,
            json.dumps(event, ensure_ascii=True),
        ),
    )
    return int(cursor.fetchone()[0])


def _ingest_schedule_catalog(cursor, client: DataGolfClient, tours: tuple[str, ...], summary: dict,
                             date_from: date | None = None, date_to: date | None = None) -> None:
    seen: set[str] = set()
    for tour in tours:
        try:
            payload = client.get_schedule(tour)
        except DataGolfError as exc:
            summary["errors"].append(f"{tour}: schedule {exc}")
            continue
        for event in _schedule_events(payload, tour):
            event_tour = str(_event_value(event, "tour", "tour_code") or tour).strip().lower() or tour
            event_date = _parse_date(_event_value(event, "date_start", "start_date", "event_date", "date"))
            if date_from and event_date and event_date < date_from:
                continue
            if date_to and event_date and event_date > date_to:
                continue
            provider_id = _catalog_provider_id(event_tour, event)
            if provider_id in seen:
                continue
            seen.add(provider_id)
            _upsert_catalog_tournament(cursor, event_tour, event)
            summary["catalog_tournaments"] += 1


def _upsert_player(cursor, dg_id: int | None, name: str, *, country: str | None = None,
                   owgr: int | None = None, dg_rank: int | None = None,
                   amateur: bool = False, raw: dict | None = None) -> int:
    """Resout/cree un joueur par dg_id (cle stable), retourne golf_player_id."""
    display = _display_name(name)
    payload = json.dumps(raw or {}, ensure_ascii=True)
    if dg_id is not None:
        cursor.execute(
            """
            INSERT INTO core.golf_players (
                player_name, provider_participant_id, country_code,
                dg_id, owgr_rank, dg_rank, amateur, raw_player_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (dg_id) WHERE dg_id IS NOT NULL DO UPDATE
            SET player_name = EXCLUDED.player_name,
                country_code = COALESCE(EXCLUDED.country_code, core.golf_players.country_code),
                owgr_rank = COALESCE(EXCLUDED.owgr_rank, core.golf_players.owgr_rank),
                dg_rank = COALESCE(EXCLUDED.dg_rank, core.golf_players.dg_rank),
                amateur = EXCLUDED.amateur,
                raw_player_json = EXCLUDED.raw_player_json,
                updated_at = now()
            RETURNING golf_player_id
            """,
            (display, str(dg_id), country, dg_id, owgr, dg_rank, amateur, payload),
        )
        return int(cursor.fetchone()[0])
    # Sans dg_id : repli sur (nom, provider_participant_id).
    cursor.execute(
        """
        INSERT INTO core.golf_players (player_name, raw_player_json)
        VALUES (%s, %s::jsonb)
        ON CONFLICT (player_name, provider_participant_id) DO UPDATE
        SET raw_player_json = EXCLUDED.raw_player_json, updated_at = now()
        RETURNING golf_player_id
        """,
        (display, payload),
    )
    return int(cursor.fetchone()[0])


def _upsert_bookmaker(cursor, key: str) -> int:
    code = key.strip().upper()
    cursor.execute(
        """
        INSERT INTO core.bookmakers (bookmaker_code, bookmaker_name, is_active)
        VALUES (%s, %s, true)
        ON CONFLICT (bookmaker_code) DO UPDATE
        SET is_active = true, updated_at = now()
        RETURNING bookmaker_id
        """,
        (code, key.strip()),
    )
    return int(cursor.fetchone()[0])


def _upsert_tournament(cursor, tour: str, field: dict) -> int:
    event_id = field.get("event_id")
    season = _season_from(field)
    provider_event_id = f"{tour}-{season}-{event_id}"
    name = str(field.get("event_name") or f"{tour} {event_id}").strip()
    date_start = _parse_date(field.get("date_start"))
    commence = datetime.combine(date_start, datetime.min.time(), tzinfo=UTC) if date_start else None
    cursor.execute(
        """
        INSERT INTO core.golf_tournaments (
            provider_event_id, sport_key, tournament_name, sport_title,
            commence_time, dg_event_id, tour_code, course_name,
            date_start, date_end, current_round, season, raw_event_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (provider_event_id) DO UPDATE
        SET tournament_name = EXCLUDED.tournament_name,
            sport_title = EXCLUDED.sport_title,
            commence_time = COALESCE(EXCLUDED.commence_time, core.golf_tournaments.commence_time),
            dg_event_id = EXCLUDED.dg_event_id,
            tour_code = EXCLUDED.tour_code,
            course_name = COALESCE(EXCLUDED.course_name, core.golf_tournaments.course_name),
            date_start = COALESCE(EXCLUDED.date_start, core.golf_tournaments.date_start),
            date_end = COALESCE(EXCLUDED.date_end, core.golf_tournaments.date_end),
            current_round = EXCLUDED.current_round,
            season = EXCLUDED.season,
            catalog_status = 'FIELD_SYNCED',
            last_field_sync_at = now(),
            raw_event_json = EXCLUDED.raw_event_json,
            updated_at = now()
        RETURNING golf_tournament_id
        """,
        (
            provider_event_id,
            f"datagolf_{tour}",
            name,
            tour.upper(),
            commence,
            int(event_id) if event_id is not None else None,
            tour,
            str(field.get("course_name") or "").strip() or None,
            date_start,
            _parse_date(field.get("date_end")),
            field.get("current_round"),
            season,
            json.dumps({k: field.get(k) for k in ("event_id", "event_name", "course_name",
                                                  "date_start", "date_end", "current_round")},
                       ensure_ascii=True),
        ),
    )
    return int(cursor.fetchone()[0])


def _ingest_field(cursor, field: dict) -> dict[int, int]:
    """Upsert des joueurs du field, retourne map dg_id -> golf_player_id."""
    dg_map: dict[int, int] = {}
    for player in field.get("field", []) or []:
        dg_id = player.get("dg_id")
        if dg_id is None:
            continue
        pid = _upsert_player(
            cursor, int(dg_id), str(player.get("player_name") or ""),
            country=str(player.get("country") or "").strip() or None,
            owgr=player.get("owgr_rank"), dg_rank=player.get("dg_rank"),
            amateur=bool(player.get("am")), raw=player,
        )
        dg_map[int(dg_id)] = pid
    return dg_map


def _resolve_player(cursor, dg_map: dict[int, int], dg_id: object, name: str) -> int | None:
    if dg_id is None:
        return None
    dg_id = int(dg_id)
    if dg_id not in dg_map:
        dg_map[dg_id] = _upsert_player(cursor, dg_id, name)
    return dg_map[dg_id]


def _outright_price(book_key: str, value: object) -> float | None:
    """Prix decimal d'une cellule outright. `datagolf` est un dict
    {baseline, baseline_history_fit} -> on prend le modele course-fit."""
    if book_key == "datagolf":
        if isinstance(value, dict):
            return _num(value.get(DATAGOLF_MODEL_FIELD)) or _num(value.get("baseline"))
        return None
    return _num(value)


def _ingest_outrights(cursor, tournament_id: int, dg_map: dict[int, int],
                      client: DataGolfClient, tour: str, summary: dict) -> None:
    captured = datetime.now(UTC)
    for dg_market, market_code in OUTRIGHT_MARKETS.items():
        try:
            payload = client.get_outrights(tour, dg_market)
        except DataGolfError:
            continue
        rows = payload.get("odds")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            pid = _resolve_player(cursor, dg_map, row.get("dg_id"), str(row.get("player_name") or ""))
            if pid is None:
                continue
            selection = _display_name(str(row.get("player_name") or ""))
            for book_key, value in row.items():
                if book_key in ("dg_id", "player_name"):
                    continue
                price = _outright_price(book_key, value)
                if price is None or price <= 1.0:
                    continue
                # `datagolf` -> book synthetique DATAGOLF (= fair du modele).
                bookmaker_id = _upsert_bookmaker(cursor, DATAGOLF_BOOK if book_key == "datagolf" else book_key)
                cursor.execute(
                    """
                    INSERT INTO core.golf_odds (
                        golf_tournament_id, golf_player_id, bookmaker_id,
                        market_code, selection_name, captured_at, decimal_odd,
                        source_system, raw_market_key
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'DATAGOLF_V1', %s)
                    ON CONFLICT (golf_tournament_id, bookmaker_id, market_code,
                                 selection_name, captured_at) DO UPDATE
                    SET decimal_odd = EXCLUDED.decimal_odd,
                        golf_player_id = EXCLUDED.golf_player_id
                    """,
                    (tournament_id, pid, bookmaker_id, market_code, selection,
                     captured, float(price), dg_market),
                )
                summary["outright_odds"] += 1


def _ingest_matchups(cursor, tournament_id: int, dg_map: dict[int, int],
                     client: DataGolfClient, tour: str, summary: dict) -> None:
    captured = datetime.now(UTC)
    for dg_market, market_code in MATCHUP_MARKETS.items():
        try:
            payload = client.get_matchups(tour, dg_market)
        except DataGolfError:
            continue
        matches = payload.get("match_list")
        if not isinstance(matches, list):
            continue
        for match in matches:
            if not isinstance(match, dict):
                continue
            p1 = _resolve_player(cursor, dg_map, match.get("p1_dg_id"), str(match.get("p1_player_name") or ""))
            p2 = _resolve_player(cursor, dg_map, match.get("p2_dg_id"), str(match.get("p2_player_name") or ""))
            if p1 is None or p2 is None:
                continue
            p3 = _resolve_player(cursor, dg_map, match.get("p3_dg_id"), str(match.get("p3_player_name") or "")) \
                if match.get("p3_dg_id") is not None else None
            ties_rule = str(match.get("ties") or "").strip() or None
            odds = match.get("odds") or {}

            # Cotes de chaque book + le fair DataGolf (book synthetique DATAGOLF,
            # deja devige nativement) : le runner prediction en tirera le modele.
            for book_key, quote in odds.items():
                if not isinstance(quote, dict):
                    continue
                bookmaker_id = _upsert_bookmaker(cursor, DATAGOLF_BOOK if book_key == "datagolf" else book_key)
                cursor.execute(
                    """
                    INSERT INTO core.golf_matchup_odds (
                        golf_tournament_id, market_code, bookmaker_id,
                        p1_golf_player_id, p2_golf_player_id, p3_golf_player_id,
                        p1_odd, p2_odd, p3_odd, tie_odd, ties_rule, captured_at, raw_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (golf_tournament_id, market_code, bookmaker_id,
                                 p1_golf_player_id, p2_golf_player_id, captured_at) DO UPDATE
                    SET p1_odd = EXCLUDED.p1_odd, p2_odd = EXCLUDED.p2_odd,
                        p3_odd = EXCLUDED.p3_odd, tie_odd = EXCLUDED.tie_odd
                    """,
                    (
                        tournament_id, market_code, bookmaker_id, p1, p2, p3,
                        _num(quote.get("p1")), _num(quote.get("p2")), _num(quote.get("p3")),
                        _num(quote.get("tie")), ties_rule, captured,
                        json.dumps(quote, ensure_ascii=True),
                    ),
                )
                summary["matchup_odds"] += 1


def _parse_position(text: object) -> tuple[int | None, bool]:
    raw = str(text or "").strip().upper()
    if raw.startswith("T"):
        raw = raw[1:]
    if raw.isdigit():
        return int(raw), True
    return None, False


def _ingest_results(cursor, client: DataGolfClient, tour: str, tournament_id: int,
                    dg_map: dict[int, int], summary: dict) -> None:
    """Positions courantes/finales via in-play -> core.golf_results (reglement)."""
    try:
        payload = client.get_in_play(tour)
    except DataGolfError:
        return
    rnd = (payload.get("info") or {}).get("current_round")
    for row in payload.get("data") or []:
        dg_id = row.get("dg_id")
        if dg_id is None:
            continue
        rank, made_cut = _parse_position(row.get("current_pos"))
        cursor.execute(
            """
            INSERT INTO core.golf_results (
                golf_tournament_id, golf_player_id, dg_id, position_text,
                position_rank, made_cut, current_round, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (golf_tournament_id, dg_id) DO UPDATE
            SET golf_player_id = COALESCE(EXCLUDED.golf_player_id, core.golf_results.golf_player_id),
                position_text = EXCLUDED.position_text, position_rank = EXCLUDED.position_rank,
                made_cut = EXCLUDED.made_cut, current_round = EXCLUDED.current_round, updated_at = now()
            """,
            (tournament_id, dg_map.get(int(dg_id)), int(dg_id),
             str(row.get("current_pos") or ""), rank, made_cut, rnd),
        )
        summary["results"] += 1


def _num(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


# Champs de proba pre-tournoi -> codes marche.
_PRED_FIELDS = {
    "win": "TOURNAMENT_WINNER",
    "top_5": "TOP_5",
    "top_10": "TOP_10",
    "top_20": "TOP_20",
    "make_cut": "MAKE_CUT",
}


def _ingest_pretournament(cursor, client: DataGolfClient, tour: str,
                          tournament_id: int, dg_map: dict[int, int],
                          summary: dict) -> None:
    """Probas pre-tournoi des DEUX modeles DataGolf -> core.golf_pretournament_preds.

    Source primaire du moteur V3 (golf_engine) : couvre TOUT le field, pas
    seulement les joueurs cotes. Le modele course-fit s'appelle
    `baseline_history_fit` (events principaux) ou `baseline_history`
    (opposite-field) — on prend celui qui existe."""
    try:
        payload = client.get_pre_tournament(tour)
    except DataGolfError as exc:
        summary["errors"].append(f"{tour}: pretournament {exc}")
        return
    baseline_rows = payload.get("baseline") or []
    fit_rows = payload.get("baseline_history_fit") or payload.get("baseline_history") or []
    fit_by_id = {int(r["dg_id"]): r for r in fit_rows if r.get("dg_id") is not None}
    # Purge idempotente : on repart de l'etat courant de l'API pour ce tournoi.
    cursor.execute(
        "DELETE FROM core.golf_pretournament_preds WHERE golf_tournament_id = %s",
        (tournament_id,),
    )
    for row in baseline_rows:
        dg_id = row.get("dg_id")
        if dg_id is None:
            continue
        dg_id = int(dg_id)
        fit = fit_by_id.get(dg_id, {})
        name = _display_name(str(row.get("player_name") or ""))
        for field_key, market in _PRED_FIELDS.items():
            p_base = row.get(field_key)
            p_fit = fit.get(field_key)
            if p_base is None and p_fit is None:
                continue
            cursor.execute(
                """
                INSERT INTO core.golf_pretournament_preds (
                    golf_tournament_id, golf_player_id, dg_id, player_name,
                    market_code, prob_baseline, prob_fit, sample_size
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (golf_tournament_id, dg_id, market_code) DO UPDATE
                SET prob_baseline = EXCLUDED.prob_baseline,
                    prob_fit = EXCLUDED.prob_fit,
                    golf_player_id = COALESCE(EXCLUDED.golf_player_id,
                                              core.golf_pretournament_preds.golf_player_id),
                    captured_at = now()
                """,
                (
                    tournament_id, dg_map.get(dg_id), dg_id, name, market,
                    float(p_base) if p_base is not None else None,
                    float(p_fit) if p_fit is not None else None,
                    row.get("sample_size"),
                ),
            )
            summary["pretournament_preds"] += 1


def _ingest_skill_ratings(cursor, client: DataGolfClient, summary: dict) -> None:
    """Couche de donnees SG (strokes-gained par categorie) — transparence + QC.
    Tour-agnostique : un seul appel. Bridge golf_player_id via dg_id."""
    try:
        payload = client.get_skill_ratings(display="value")
    except DataGolfError as exc:
        summary["errors"].append(f"skill-ratings {exc}")
        return
    for row in payload.get("players") or []:
        dg_id = row.get("dg_id")
        if dg_id is None:
            continue
        cursor.execute(
            """
            INSERT INTO core.golf_skill_ratings (
                dg_id, golf_player_id, player_name, sg_total, sg_ott, sg_app,
                sg_arg, sg_putt, driving_acc, driving_dist, updated_at
            )
            VALUES (%s, (SELECT golf_player_id FROM core.golf_players WHERE dg_id = %s),
                    %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (dg_id) DO UPDATE
            SET golf_player_id = COALESCE(EXCLUDED.golf_player_id, core.golf_skill_ratings.golf_player_id),
                player_name = EXCLUDED.player_name,
                sg_total = EXCLUDED.sg_total, sg_ott = EXCLUDED.sg_ott,
                sg_app = EXCLUDED.sg_app, sg_arg = EXCLUDED.sg_arg,
                sg_putt = EXCLUDED.sg_putt, driving_acc = EXCLUDED.driving_acc,
                driving_dist = EXCLUDED.driving_dist, updated_at = now()
            """,
            (
                int(dg_id), int(dg_id), _display_name(str(row.get("player_name") or "")),
                _num(row.get("sg_total")), _num(row.get("sg_ott")), _num(row.get("sg_app")),
                _num(row.get("sg_arg")), _num(row.get("sg_putt")),
                _num(row.get("driving_acc")), _num(row.get("driving_dist")),
            ),
        )
        summary["skill_ratings"] += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Synchronisation DataGolf par catalogue/tour.")
    parser.add_argument("--tour", action="append", help="Tour DataGolf a synchroniser (repete possible).")
    parser.add_argument("--catalog-only", action="store_true", help="Ne synchronise que le catalogue schedule.")
    parser.add_argument("--skip-catalog", action="store_true", help="Ignore le schedule catalog.")
    parser.add_argument("--date-from", type=_valid_date_arg, default=None)
    parser.add_argument("--date-to", type=_valid_date_arg, default=None)
    args = parser.parse_args()

    settings = DataGolfSettings.from_env()
    active_tours = tuple(args.tour) if args.tour else settings.tours
    catalog_tours = tuple(args.tour) if args.tour else settings.catalog_tours
    summary = {
        "catalog_tours": [], "catalog_tournaments": 0,
        "tours": [], "tournaments": 0, "players": 0,
        "outright_odds": 0, "matchup_odds": 0, "skill_ratings": 0,
        "pretournament_preds": 0, "results": 0, "errors": [],
    }
    if not settings.enabled:
        summary["errors"].append("DATAGOLF_API_KEY manquant")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    client = DataGolfClient(settings)
    try:
        with connection.cursor() as cursor:
            if not args.skip_catalog:
                summary["catalog_tours"] = list(catalog_tours)
                _ingest_schedule_catalog(cursor, client, catalog_tours, summary, args.date_from, args.date_to)
            if args.catalog_only:
                connection.commit()
                print(json.dumps(summary, indent=2, ensure_ascii=False))
                return 0
            for tour in active_tours:
                try:
                    field = client.get_field_updates(tour)
                except DataGolfError as exc:
                    summary["errors"].append(f"{tour}: field {exc}")
                    continue
                if not field.get("field") or field.get("event_id") is None:
                    continue
                tournament_id = _upsert_tournament(cursor, tour, field)
                summary["tournaments"] += 1
                summary["tours"].append(tour)
                dg_map = _ingest_field(cursor, field)
                summary["players"] += len(dg_map)

                _ingest_pretournament(cursor, client, tour, tournament_id, dg_map, summary)
                _ingest_outrights(cursor, tournament_id, dg_map, client, tour, summary)
                _ingest_matchups(cursor, tournament_id, dg_map, client, tour, summary)
                _ingest_results(cursor, client, tour, tournament_id, dg_map, summary)
            # Couche SG (transparence + QC) : un seul appel, tous joueurs.
            _ingest_skill_ratings(cursor, client, summary)
        connection.commit()
    finally:
        connection.close()
        client.close()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
