# Cahier produit V1

## 1. Objet

Ce document formalise les attentes fonctionnelles de la `Version 1`.

La V1 correspond à un moteur d'analyse pré-match de football qui classe les meilleures opportunités sur le marché `1X2`.

## 2. Périmètre fonctionnel

### Inclus

- football uniquement ;
- analyse pré-match ;
- marché bookmaker `1X2` uniquement ;
- zone d'analyse paramétrable ;
- ingestion de données historiques et pré-match ;
- première couche de signaux de contexte ;
- génération de probabilités `home/draw/away` ;
- calcul d'edge et scoring de deal ;
- dashboard de consultation ;
- historique et backtesting.

### Exclus

- paris live ;
- autres sports ;
- autres marchés ;
- exécution automatique de mises ;
- fonctionnalités communautaires ;
- système de paiement avancé ;
- architecture haute disponibilité multi-régions.

## 3. Utilisateurs cibles

### Analyste parieur avancé

Cherche à filtrer rapidement un univers de matchs et à repérer les écarts intéressants.

### Opérateur interne / trader analytique

Cherche à industrialiser une logique de détection d'opportunités et à mesurer sa performance.

### Prospect business / partenaire

Cherche à comprendre la valeur du moteur et sa capacité à devenir un produit monétisable.

## 4. Cas d'usage principaux

### UC-01 Définir une zone d'analyse

L'utilisateur doit pouvoir définir un périmètre de travail.

Exemples :

- pays ;
- ligues ;
- fenêtre temporelle ;
- bookmakers ciblés ;
- compétitions actives.

### UC-02 Lancer ou consulter une analyse

Le système doit agréger tous les matchs de football correspondant au périmètre choisi.

### UC-03 Obtenir des probabilités `1X2`

Pour chaque match, le système doit calculer :

- `probabilité victoire domicile`
- `probabilité match nul`
- `probabilité victoire extérieur`

### UC-04 Comparer au marché bookmaker

Le système doit transformer les cotes `1X2` en probabilités implicites et comparer ces valeurs au modèle.

### UC-05 Classer les meilleurs deals

Le système doit ordonner les opportunités selon une logique de ranking explicite.

### UC-06 Justifier une recommandation

Chaque deal remonté doit être accompagné d'une justification synthétique :

- edge estimé ;
- niveau de confiance ;
- principaux facteurs ayant influencé l'évaluation ;
- état de qualité des données.

### UC-07 Évaluer la performance historique

Le système doit permettre de comparer :

- recommandations passées ;
- résultats réels ;
- performance du modèle ;
- stabilité du ranking.

## 5. Entrées produit

### Données sportives de base

- compétitions ;
- équipes ;
- fixtures ;
- résultats passés ;
- statistiques par match ;
- classements si disponibles.

### Données de marché

- cotes `1X2` par bookmaker ;
- horodatage des snapshots ;
- variations de cote.

Décision V1 :

- bookmaker unique de référence : `bet365`
- objectif : réduire le bruit inter-bookmakers et garder une base cohérente pour le ranking initial

Note V1 base de donnees :

- `TheSportsDB` couvre la donnée football de reference
- la documentation officielle consultee ne documente pas de flux bookmaker `1X2`
- le schema stocke donc les odds, mais leur ingestion necessitera une source complementaire

### Données contextuelles

- absences et suspensions si disponibles ;
- densité du calendrier ;
- facteur domicile / extérieur ;
- dynamique récente ;
- qualité et fraîcheur du signal.

## 6. Sorties attendues

Pour chaque match analysé, la V1 doit retourner :

- identité du match ;
- heure ;
- compétition ;
- sélection recommandée ;
- bookmaker ;
- cote ;
- probabilité modèle ;
- probabilité implicite ;
- edge ;
- score de ranking ;
- niveau de confiance ;
- justification courte.

## 7. Règles de gestion

- une recommandation n'existe que si une cote bookmaker `1X2` est disponible ;
- les données doivent être datées ;
- les prédictions doivent être versionnées ;
- une modification de données importantes doit permettre un recalcul ;
- les opportunités doivent être ordonnées, pas juste listées.

## 8. Définition de succès V1

La V1 est fonctionnellement réussie si :

1. l'utilisateur peut définir un périmètre d'analyse ;
2. les matchs pertinents sont ingérés proprement ;
3. chaque match reçoit une estimation `1X2` ;
4. les meilleures opportunités sont classées ;
5. l'utilisateur peut comprendre et revoir les recommandations.
