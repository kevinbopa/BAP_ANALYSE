# Audit front production V1

Objectif : transformer chaque page du Sports Prediction Engine en interface client vendable, maintenable et auditable. Ce document sert de grille de controle avant chaque livraison front.

## Direction visuelle

Le produit doit inspirer confiance avant d'impressionner. La direction retenue est Swiss produit : fond clair, grille discrete, typographie Helvetica, filets 1px, accent rouge pour les decisions importantes, aucune decoration gratuite.

Regle centrale : si un element ne clarifie pas une decision, une performance, un filtre, une action ou un risque, il doit etre retire ou deplace.

## Carte des sections

| Section | Role client | Controle visuel attendu | Maintenance |
|---|---|---|---|
| Navigation | Choisir sport et page active | Un seul systeme de nav, sport + onglets, aucun lien mort | Tout changement de page passe par `render_sport_nav` |
| Hero | Identifier le contexte | Titre clair, sous-texte court, visuel sport non decoratif | Aucun badge temporaire ou wording interne |
| Actions | Lancer ingestion/prediction/cycle | Boutons alignes, action principale visible, libelles standards | Chaque bouton doit correspondre a une action serveur testee |
| Filtres | Reduire la masse de donnees | Meme logique jour/semaine/mois/tout, meme apparence foot/golf | Les filtres doivent rester dans les query params |
| Metrics | Donner l'etat rapide | Chiffres tabulaires, cartes coherentes, libelles courts | Pas de metrique sans source data claire |
| Predictions | Lire la probabilite modele | Tables scannables, tri et confiance visibles | Toute colonne ajoutee doit etre utile a la decision |
| Deals | Choisir quoi jouer | Pari explicite, cote, modele, edge, prise, nombre de prises | Les prises doivent rester liees aux tickets en base |
| Strategie | Transformer les deals en mises | Profil prudent/equilibre/agressif + scope visible | Chaque strategie doit etre testable sur bankroll definie |
| Back | Verifier le reel | Resultat, profit reel, positions prises, filtres contextuels | Validation back sans relancer toute la prediction |
| Detail match/tournoi | Comprendre pourquoi | Explication, historique, cotes, confrontation, signaux | Garder les sections dans un ordre stable |
| Footer legal | Cadrer le risque | Disclaimer visible, maintenance, vision produit | Footer unique via `render_product_footer` |

## Grille de revue par section

### 1. Disposition

- Les bords gauches doivent s'aligner entre nav, hero, actions, filtres, sections et footer.
- Les sections doivent suivre une cadence verticale stable : nav, hero, actions/filtres, contenu, footer.
- Les tables longues doivent rester dans `.table-wrap` pour conserver le scroll horizontal.
- Les pages detail ne doivent pas introduire une autre navigation que la nav globale ou un retour simple.

### 2. Filtres

- Football et golf doivent partager la meme logique de periode quand le contexte est comparable.
- Le filtre sport ne doit pas etre visible dans Back : le sport vient de la nav.
- Les filtres doivent etre persistants dans l'URL pour rendre l'etat partageable.
- Les libelles doivent nommer l'effet reel : `Jour`, `Semaine`, `Mois`, `Tout`, `Prises seulement`, etc.

### 3. Images et representation

- Aucun visuel generique de banque d'images dans la V1.
- Les visuels doivent rester abstraits, sportifs et non trompeurs : terrain football, green golf, grille analytique.
- Pas d'icone unicode decorative pour remplacer une vraie information.
- Le hero ne doit jamais prendre plus d'importance que les decisions.

### 4. Textes

- Les titres doivent dire ce que la page permet de faire.
- Les sous-textes doivent rester courts : une phrase utile maximum.
- Eviter les termes internes : `ops`, `a cadrer`, `debug`, `test`, `local` visibles client.
- Les boutons utilisent des verbes standards : `Afficher`, `Filtrer`, `Prendre`, `Supprimer`, `Lancer`.

### 5. Alignements

- Les controles d'une meme ligne doivent avoir la meme hauteur.
- Les chiffres de performance doivent utiliser les numeraux tabulaires.
- Les notes de section doivent rester a droite sur desktop, puis passer sous le titre sur mobile si necessaire.
- Les badges `PRIS xN` doivent rester dans la colonne de decision, pas disperses dans le texte.

### 6. Bas de page legal

- Chaque page principale doit afficher le footer commun.
- Le footer doit rappeler que le produit donne une probabilite, pas une garantie.
- Le footer doit indiquer que la decision finale appartient a l'utilisateur.
- Le footer doit rappeler la dependance aux fournisseurs API et aux migrations.

### 7. Vision maintenance

- `BASE_CSS` reste la source du systeme visuel.
- `render_section`, `render_sport_nav` et `render_product_footer` sont les composants globaux a reutiliser.
- Toute nouvelle page doit inclure : nav, hero, contenu, footer, tests smoke.
- Toute nouvelle fonctionnalite front doit ajouter ou mettre a jour un test smoke si elle change la navigation, les filtres, les prises ou le back.

## Priorites restantes

1. Extraire progressivement le CSS et les composants HTML hors de `app.py` quand la migration Next.js sera decidee.
2. Ajouter un vrai systeme d'authentification client/admin avant de montrer un espace admin.
3. Faire une passe responsive complete sur mobile/tablette avec captures.
4. Ajouter des tests smoke par route : football predictions, football deals, golf predictions, golf deals, strategie foot, strategie golf, back foot, back golf, detail match, detail tournoi.

## Passe appliquee - 2026-07-11

Changements front termines :

- Footer legal/maintenance commun ajoute aux pages principales.
- Etats focus visibles ajoutes aux controles interactifs.
- Formulaires de prise et tickets migres vers classes communes.
- Back : filtres, validation, notes et tickets homogenises.
- Strategie football : controle bankroll/periode/competition/profil migre vers `filterbar`.
- Strategie golf : profils, bankroll et scope nettoyes visuellement.
- Responsive : sections, actions et footer mieux structures sur mobile.

Dette restante acceptee temporairement :

- Quelques styles inline techniques restent pour les largeurs de rail de probabilite et colonnes compactes de forme.
- Le fichier `apps/dashboard/app.py` concentre encore trop de HTML/CSS; il faudra extraire les composants avant une vraie migration Next.js/Vercel.
