# Cadrage fonctionnel V1

## Définition produit

La V1 n'est pas seulement un moteur qui prédit des scores. C'est un moteur qui parcourt tous les matchs de football d'une zone définie par l'utilisateur, analyse le marché `1X2` proposé par les bookmakers, puis classe les meilleurs deals selon les probabilités estimées par le système.

## Fonction principale

Le système doit répondre à la question suivante :

Parmi les matchs et marchés visibles dans une zone choisie par l'utilisateur, quelles sont les meilleures opportunités actuellement disponibles chez les bookmakers ?

Pour la V1, cette question devient :

Parmi les matchs de football visibles dans une zone choisie par l'utilisateur, quelles sont les meilleures opportunités sur `victoire domicile`, `match nul` ou `victoire extérieur` ?

## Zone définie par l'utilisateur

La zone d'analyse représente le périmètre dans lequel le moteur doit travailler.

Elle peut inclure :

- un pays ;
- une ligue ou un groupe de ligues ;
- une saison ;
- une plage horaire ;
- un ensemble de bookmakers ;
- un type de marchés à surveiller.

Exemples :

- football Europe aujourd'hui ;
- Ligue 1 + Premier League sur les 48 prochaines heures ;
- tous les matchs couverts par un bookmaker donné ;
- seulement le marché `1X2`.

## Ce que la V1 doit analyser

### 1. Données sportives historiques

- forme récente ;
- confrontations passées ;
- performance domicile / extérieur ;
- buts marqués et encaissés ;
- statistiques de match ;
- dynamique de classement.

### 2. Informations de dernière minute

- blessures ;
- suspensions ;
- absences de titulaires ;
- changements de lineups ;
- fatigue ou enchaînement des matchs ;
- changements météo si disponibles ;
- variations importantes des cotes.

### 3. Informations extra-sportives

La V1 peut les intégrer de manière progressive, même si la couverture est partielle au départ.

Exemples :

- motivation particulière d'un match ;
- contexte de derby ou rivalité ;
- rotation probable ;
- pression calendrier ;
- enjeux de qualification, maintien ou titre ;
- rumeurs ou événements de contexte si une source fiable existe.

## Sortie attendue

Le système ne doit pas seulement produire une probabilité brute. Il doit produire une liste priorisée d'opportunités.

Chaque opportunité doit idéalement contenir :

- le match ;
- le marché ;
- la sélection ;
- le bookmaker ;
- la cote disponible ;
- la probabilité du modèle ;
- la probabilité implicite du marché ;
- l'edge estimé ;
- un score de confiance ;
- une justification synthétique.

## Marchés prioritaires V1

Pour éviter toute dispersion, la V1 commence uniquement par :

- `1X2`

Sous-sélections analysées :

- victoire domicile ;
- match nul ;
- victoire extérieur.

## Logique de classement des deals

Le classement final peut reposer sur un score composite.

Exemple de dimensions :

- edge entre probabilité modèle et marché ;
- confiance du modèle ;
- stabilité de la cote ;
- qualité des données disponibles ;
- liquidité ou crédibilité du bookmaker ;
- cohérence avec d'autres signaux.

## Priorités de conception

- couvrir large sur les matchs de la zone ;
- rester strict sur la qualité des signaux remontés ;
- éviter les faux positifs trop agressifs ;
- rendre chaque recommandation explicable ;
- permettre un backtesting propre des recommandations.

## Définition simple de la V1 réussie

La V1 est réussie si un utilisateur peut :

1. définir son périmètre d'analyse ;
2. lancer ou consulter l'analyse de tous les matchs concernés ;
3. voir les marchés intéressants classés par priorité ;
4. comprendre pourquoi un deal est recommandé ;
5. mesurer après coup si les recommandations étaient bonnes.
