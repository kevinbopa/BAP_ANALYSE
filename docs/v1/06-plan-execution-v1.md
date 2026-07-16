# Plan d'exécution V1

## 1. Objectif

Déployer une V1 propre, démontrable et techniquement saine.

## 2. Phasage recommandé

### Phase A. Dossier et cadrage

Livrables :

- dossier V1 validé ;
- périmètre football `1X2` verrouillé ;
- définition des zones d'analyse ;
- stratégie fournisseur initiale.

### Phase B. Fondation technique

Livrables :

- structure du dépôt ;
- environnement local ;
- configuration projet ;
- base PostgreSQL ;
- migrations initiales.

### Phase C. Ingestion football

Livrables :

- connecteur API ;
- ingestion des ligues ;
- ingestion des équipes ;
- ingestion des fixtures ;
- ingestion des résultats ;
- ingestion des cotes `1X2` si une source complémentaire est ajoutée à TheSportsDB.

### Phase D. Modèle V1

Livrables :

- pipeline de features ;
- baseline Poisson ;
- baseline Elo ;
- stockage des prédictions ;
- rapport d'évaluation initial.

### Phase E. Opportunity et ranking

Livrables :

- calcul des probabilités implicites ;
- calcul d'edge ;
- moteur de scoring ;
- classement final des deals.

### Phase F. API et dashboard

Livrables :

- endpoints produit ;
- vue matchs ;
- vue opportunités ;
- vue historique ;
- démonstrateur utilisable.

### Phase G. Backtesting et crédibilité business

Livrables :

- rapports de performance ;
- cas d'usage démontrables ;
- éléments de présentation commerciale ;
- base pour itération V2.

## 3. Ordre de priorité de construction

1. schéma de base de données
2. ingestion des matchs
3. ingestion des cotes
4. génération des features
5. modèle `1X2`
6. détection d'opportunités
7. ranking
8. API
9. dashboard
10. backtesting approfondi

## 4. Risques principaux

### Risque data

Qualité ou couverture insuffisante des fournisseurs.

Réponse :

- privilégier une source stable pour démarrer ;
- journaliser les manques ;
- construire un modèle tolérant aux zones moins riches ;
- séparer clairement la source football `TheSportsDB` et la future source de cotes.

### Risque produit

Trop de complexité trop tôt.

Réponse :

- rester strictement sur le football `1X2` ;
- ne pas disperser la V1 ;
- viser l'utilité avant l'exhaustivité.

### Risque business

Promesse commerciale plus grande que la réalité V1.

Réponse :

- présenter la V1 comme une plateforme fondatrice ;
- insister sur la traçabilité et la méthode ;
- démontrer la progression mesurable.

## 5. Critères de passage en V2

La V2 devient légitime si :

- la V1 produit un historique utile ;
- le ranking montre une valeur réelle ;
- la couverture data est satisfaisante ;
- le dashboard devient un support crédible de démonstration ;
- les extensions futures ont une logique claire.
