# Dossier Version 1

## Objectif du dossier

Ce dossier constitue la référence officielle de la `Version 1` du produit `Sports Prediction Engine`.

Il a trois usages :

- cadrer précisément ce qui doit être construit ;
- aligner produit, technique et exploitation ;
- servir de base crédible pour présenter, vendre ou financer le projet.

## Positionnement de la V1

La V1 est une plateforme d'analyse de matchs de football focalisée sur un seul marché bookmaker : `1X2`.

Le produit permet à un utilisateur de :

- définir une zone d'analyse ;
- collecter et consolider les données des matchs de football de cette zone ;
- estimer les probabilités `victoire domicile`, `match nul`, `victoire extérieur` ;
- comparer ces probabilités aux cotes bookmakers ;
- classer les meilleures opportunités détectées.

## Table des documents

- [Résumé exécutif](./01-resume-executif.md)
- [Cahier produit V1](./02-cahier-produit-v1.md)
- [Architecture solution V1](./03-architecture-solution-v1.md)
- [Stratégie données et modèles](./04-strategie-donnees-et-modeles.md)
- [Exigences non fonctionnelles](./05-exigences-non-fonctionnelles.md)
- [Plan d'exécution V1](./06-plan-execution-v1.md)
- [Valeur business et stratégie produit](./07-valeur-business-et-strategie-produit.md)
- [Moteur algorithmique V1](./08-moteur-algorithmique-v1.md)
- [Stratégie test, capital et validation V1](./09-strategie-test-capital-et-validation.md)
- [Base de donnees V1](../../db/README.md)

## Résultat attendu de la V1

À la fin de la V1, le produit doit être capable de fonctionner comme un moteur de sélection d'opportunités sur le football pré-match.

Le système doit produire pour chaque match pertinent :

- des probabilités `1X2` ;
- une comparaison contre le marché bookmaker ;
- un score d'opportunité ;
- un classement final des deals ;
- une justification exploitable par l'utilisateur.

## Ce que la V1 ne cherche pas encore à faire

La V1 ne vise pas :

- le live betting à haute fréquence ;
- l'analyse multi-sports ;
- les marchés secondaires complexes ;
- le trading automatisé ;
- l'infrastructure distribuée avancée.

## Principes directeurs

- simplicité fonctionnelle ;
- qualité des données ;
- explicabilité des signaux ;
- architecture extensible ;
- crédibilité business.
