# Exigences non fonctionnelles

## 1. Objet

Ce document définit les attentes de qualité de la V1 au-delà des simples fonctionnalités.

## 2. Fiabilité

Le système doit :

- éviter les doublons d'ingestion ;
- gérer les indisponibilités fournisseurs ;
- conserver l'historique des traitements ;
- permettre une reprise après échec ;
- versionner les sorties importantes.

## 3. Performance

Pour la V1, la performance recherchée est pragmatique.

Le produit doit être capable de :

- recalculer un lot de matchs d'une zone en quelques minutes ;
- servir le dashboard avec une latence raisonnable ;
- supporter plusieurs recalculs quotidiens ;
- historiser les résultats sans dégradation immédiate.

## 4. Qualité de données

Le système doit :

- dater toutes les données importantes ;
- identifier la source fournisseur ;
- conserver les payloads bruts importants ;
- journaliser les anomalies de mapping ;
- signaler les données partielles ou faibles.

## 5. Auditabilité

Chaque recommandation importante doit être retraçable par :

- version du modèle ;
- timestamp de calcul ;
- snapshot de cote ;
- données principales utilisées ;
- logique de scoring appliquée.

## 6. Sécurité

La V1 doit au minimum :

- isoler les secrets API ;
- séparer configuration et code ;
- éviter d'exposer des clés côté client ;
- protéger les endpoints d'administration si présents ;
- journaliser les accès sensibles.

## 7. Maintenabilité

Le projet doit rester lisible et modulaire.

Attentes :

- séparation claire par domaine ;
- conventions de nommage stables ;
- documentation à jour ;
- migration de base versionnée ;
- tests minimum sur les composants critiques.

## 8. Observabilité

Le système doit produire :

- logs d'ingestion ;
- logs d'erreur ;
- suivi des exécutions de modèles ;
- métriques simples de couverture ;
- visibilité sur les échecs de pipeline.

## 9. Explicabilité

Le produit doit pouvoir expliquer pourquoi un deal apparaît.

Cela implique :

- score de confiance ;
- logique de ranking documentée ;
- justification textuelle simple ;
- distinction entre données certaines et signaux faibles.

## 10. Évolutivité

La V1 doit préparer sans douleur :

- d'autres ligues ;
- d'autres marchés ;
- des données live ;
- un moteur temps réel ;
- une UI plus premium ;
- une infrastructure plus segmentée.
