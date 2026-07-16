-- Dedoublonnage retroactif du track record.
--
-- Pendant le developpement, chaque run de pipeline a re-insere des value bets
-- pour les memes fixtures (versions successives du modele, dont les versions
-- cassees pre-fix). Regle : pour chaque fixture, seuls les bets de la DERNIERE
-- prediction (la recommandation finale avant kickoff) comptent au track
-- record. Tous les autres sont annules (CANCELLED / VOID / profit 0), meme
-- s'ils avaient deja ete regles WON/LOST — ce sont des iterations de dev,
-- pas des recommandations du systeme.
--
-- Idempotent. Le mecanisme systemique (supersede-on-write dans le repository)
-- empeche toute nouvelle pollution.

BEGIN;

WITH latest_per_fixture AS (
    SELECT vb.fixture_id, MAX(p.generated_at) AS last_generated
    FROM model.value_bets vb
    JOIN model.predictions p ON p.prediction_id = vb.prediction_id
    GROUP BY vb.fixture_id
),
bets_to_keep AS (
    SELECT vb.value_bet_id
    FROM model.value_bets vb
    JOIN model.predictions p ON p.prediction_id = vb.prediction_id
    JOIN latest_per_fixture l
      ON l.fixture_id = vb.fixture_id
     AND p.generated_at = l.last_generated
)
UPDATE model.value_bets vb
SET status_code = 'CANCELLED',
    result_code = 'VOID',
    profit_units = 0,
    settled_at = COALESCE(vb.settled_at, now())
WHERE vb.value_bet_id NOT IN (SELECT value_bet_id FROM bets_to_keep)
  AND vb.result_code IS DISTINCT FROM 'VOID';

SELECT COUNT(*) FILTER (WHERE result_code = 'VOID') AS annules,
       COUNT(*) FILTER (WHERE result_code IN ('WON','LOST')) AS comptabilises,
       COUNT(*) FILTER (WHERE result_code IS NULL) AS en_attente
FROM model.value_bets;

COMMIT;
