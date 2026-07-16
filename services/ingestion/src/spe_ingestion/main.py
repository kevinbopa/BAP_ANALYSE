from __future__ import annotations

from spe_ingestion.clients.stake import StakeClient
from spe_ingestion.clients.theoddsapi import TheOddsApiClient
from spe_ingestion.clients.thesportsdb import TheSportsDBClient
from spe_ingestion.config import StakeSettings, TheOddsApiSettings, TheSportsDBSettings
from spe_ingestion.db import DatabaseSettings, connect_db
from spe_ingestion.stake_odds_ingestor import StakeOddsIngestor
from spe_ingestion.theoddsapi_odds_ingestor import TheOddsApiOddsIngestor
from spe_ingestion.thesportsdb_ingestor import TheSportsDBIngestor


def build_thesportsdb_client() -> TheSportsDBClient:
    return TheSportsDBClient(TheSportsDBSettings.from_env())


def build_stake_client() -> StakeClient:
    return StakeClient(StakeSettings.from_env())


def build_theoddsapi_client() -> TheOddsApiClient:
    return TheOddsApiClient(TheOddsApiSettings.from_env())


def ingest_thesportsdb_reference_data():
    client = build_thesportsdb_client()
    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    try:
        ingestor = TheSportsDBIngestor(client)
        return ingestor.ingest_reference_bundle(connection)
    finally:
        connection.close()
        client.close()


def ingest_stake_odds_data():
    client = build_stake_client()
    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    try:
        ingestor = StakeOddsIngestor(client, StakeSettings.from_env())
        return ingestor.ingest_upcoming_1x2(connection)
    finally:
        connection.close()
        client.close()


def ingest_theoddsapi_odds_data():
    client = build_theoddsapi_client()
    connection = connect_db(DatabaseSettings.from_env(prefix="POSTGRES_INGEST"))
    try:
        ingestor = TheOddsApiOddsIngestor(client, TheOddsApiSettings.from_env())
        return ingestor.ingest_upcoming_1x2(connection)
    finally:
        connection.close()
        client.close()
