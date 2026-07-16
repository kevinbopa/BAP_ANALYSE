-- Fusionne les fixtures synthetiques (thesportsdb_event_id < 0, crees par
-- l'ingesteur de cotes avant l'arrivee des vrais evenements TheSportsDB)
-- avec leurs jumeaux reels. Les equipes synthetiques (thesportsdb_team_id < 0)
-- etant distinctes des vraies, l'appariement se fait par NOM d'equipe
-- normalise + kickoff identique.
--
-- Les cotes migrent vers le fixture reel ; la lignee modele du synthetique est
-- purgee (regenerable) ; fixtures, equipes et ligues synthetiques orphelines
-- sont supprimees. Idempotent.

BEGIN;

CREATE TEMP TABLE fixture_merge_pairs AS
SELECT DISTINCT ON (s.fixture_id)
    s.fixture_id AS synthetic_id,
    r.fixture_id AS real_id
FROM core.fixtures s
JOIN core.teams sh ON sh.team_id = s.home_team_id
JOIN core.teams sa ON sa.team_id = s.away_team_id
JOIN core.fixtures r
  ON r.kickoff_utc IS NOT DISTINCT FROM s.kickoff_utc
 AND r.fixture_id <> s.fixture_id
 AND r.thesportsdb_event_id > 0
JOIN core.teams rh ON rh.team_id = r.home_team_id
JOIN core.teams ra ON ra.team_id = r.away_team_id
WHERE s.thesportsdb_event_id < 0
  -- Normalisation agressive : "Bosnia & Herzegovina" == "Bosnia-Herzegovina"
  AND regexp_replace(lower(rh.team_name), '[^a-z0-9]', '', 'g')
      = regexp_replace(lower(sh.team_name), '[^a-z0-9]', '', 'g')
  AND regexp_replace(lower(ra.team_name), '[^a-z0-9]', '', 'g')
      = regexp_replace(lower(sa.team_name), '[^a-z0-9]', '', 'g')
ORDER BY s.fixture_id, r.fixture_id;

-- 1. Migrer les cotes vers le fixture reel
UPDATE core.fixture_odds_1x2 o
SET fixture_id = p.real_id
FROM fixture_merge_pairs p
WHERE o.fixture_id = p.synthetic_id;

-- 2. Purger la lignee modele du synthetique (regenerable par la pipeline)
DELETE FROM model.deal_rankings d
USING fixture_merge_pairs p
WHERE d.fixture_id = p.synthetic_id;

DELETE FROM model.value_bets vb
USING fixture_merge_pairs p
WHERE vb.fixture_id = p.synthetic_id;

DELETE FROM model.predictions pr
USING fixture_merge_pairs p
WHERE pr.fixture_id = p.synthetic_id;

-- 3. Purger score + fixture synthetique
DELETE FROM core.fixture_scores fs
USING fixture_merge_pairs p
WHERE fs.fixture_id = p.synthetic_id;

DELETE FROM core.fixtures f
USING fixture_merge_pairs p
WHERE f.fixture_id = p.synthetic_id;

SELECT (SELECT COUNT(*) FROM fixture_merge_pairs) AS fixtures_fusionnes;

-- 4. Equipes synthetiques desormais orphelines (aucun fixture ne les reference)
DELETE FROM core.teams t
WHERE t.thesportsdb_team_id < 0
  AND NOT EXISTS (
      SELECT 1 FROM core.fixtures f
      WHERE f.home_team_id = t.team_id OR f.away_team_id = t.team_id
  );

-- 5. Ligues synthetiques orphelines
DELETE FROM core.leagues l
WHERE l.thesportsdb_league_id < 0
  AND NOT EXISTS (SELECT 1 FROM core.fixtures f WHERE f.league_id = l.league_id);

COMMIT;
