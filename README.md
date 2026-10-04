# Panier Malin

Données de l'appli Android **Panier Malin** : promos des E.Leclerc Le Houlme et Bapeaume, recettes avec les produits en promo, et infos du Houlme.

- `data/panier_malin.json` : le fichier lu par l'appli, mis à jour automatiquement toutes les 3 heures (6h–21h) par GitHub Actions.
- `scripts/collecte.py` : le script de collecte (123catalogue.fr, PanneauPocket, le-houlme.fr).
- `scripts/recettes_base.json` : la base de recettes dans laquelle le script choisit celles qui utilisent les promos.

Lancer une collecte à la main : onglet **Actions** › *Collecte Panier Malin* › **Run workflow**.

Adresse du fichier pour l'appli :
`https://raw.githubusercontent.com/davidbdesk76-hash/panier-malin/main/data/panier_malin.json`
