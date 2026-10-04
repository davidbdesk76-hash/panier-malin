"""
Panier Malin - collecte automatique (GitHub Actions, sans IA)

- Promos des E.Leclerc Le Houlme et Bapeaume (catalogues e.leclerc : page du magasin + API des catalogues)
- Recettes simples choisies dans scripts/recettes_base.json selon les promos, avec leur prix de revient
  (prix normaux : vos tickets > moyennes Open Prices > estimations de scripts/prix_reference.json)
- Infos du Houlme : PanneauPocket + actualités et agenda de le-houlme.fr

Écrit data/panier_malin.json. Une partie qui échoue garde les données précédentes.
Copies HTML brutes dans debug/ quand une lecture ne donne rien (ou si debug/.actif existe).
"""

import datetime as dt
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

RACINE = Path(__file__).resolve().parent.parent
SORTIE = RACINE / "data" / "panier_malin.json"
DEBUG = RACINE / "debug"
TZ = ZoneInfo("Europe/Paris")
MAINTENANT = dt.datetime.now(TZ)
AUJOURDHUI = MAINTENANT.date()

MAGASINS = [
    {"id": "houlme", "nom": "Le Houlme", "url": "https://www.e.leclerc/mag/e-leclerc-le-houlme"},
    {"id": "bapeaume", "nom": "Bapeaume", "url": "https://www.e.leclerc/mag/e-leclerc-bapeaume"},
]
CATALOGUE_SITE = "https://nos-catalogues-promos-v2.e.leclerc"
CATALOGUE_API = "https://nos-catalogues-promos-v2-api.e.leclerc"
RE_LIEN_CATALOGUE = re.compile(r"nos-catalogues-promos-v2\.e\.leclerc/catalog/([A-Za-z0-9]+)/(\d+)")
CATALOGUES_EXCLUS = re.compile(r"voyage|s[ée]jour|croisi[èe]re", re.I)
SOURCES_VILLE = {
    "panneaupocket": "https://app.panneaupocket.com/ville/1188581073-le-houlme-76770",
    "actus": "https://www.le-houlme.fr/5930-toutes-les-actualites.htm",
    "agenda": "https://www.le-houlme.fr/5943-tout-l-agenda.htm",
}
PRATIQUE = {"mairie_adresse": "7 place des Canadiens, 76770 Le Houlme",
            "mairie_tel": "02 35 74 11 04", "facebook": "Ville du Houlme"}
CATEGORIES_CUISINE = {"viande_poisson", "fruits_legumes", "cremerie", "epicerie", "surgeles"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})

avertissements = []
debug_actif = (DEBUG / ".actif").exists()


# ------------------------------------------------------------------ outils

def norm(txt):
    txt = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", txt.lower()).strip()


def propre(txt):
    return re.sub(r"\s+", " ", txt or "").strip()


def court_id(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:8]


def lire(url, nom_debug):
    r = SESSION.get(url, timeout=40)
    r.raise_for_status()
    html = r.text
    if debug_actif:
        sauver_debug(nom_debug, html)
    return BeautifulSoup(html, "html.parser"), html


def sauver_debug(nom, html):
    DEBUG.mkdir(exist_ok=True)
    (DEBUG / f"{nom}.html").write_text(html, encoding="utf-8")


MOIS = {"janv": 1, "jan": 1, "janvier": 1, "fevr": 2, "fev": 2, "fevrier": 2, "mars": 3, "avr": 4, "avril": 4,
        "mai": 5, "juin": 6, "juil": 7, "juillet": 7, "aout": 8, "sept": 9, "sep": 9, "septembre": 9,
        "oct": 10, "octobre": 10, "nov": 11, "novembre": 11, "dec": 12, "decembre": 12}
RE_DATE_NUM = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
RE_DATE_TXT = re.compile(r"\b(\d{1,2})(?:er)?\s+(janv(?:ier)?|f[ée]vr?(?:ier)?|mars|avr(?:il)?|mai|juin|juil(?:let)?|ao[uû]t|sept?(?:embre)?|oct(?:obre)?|nov(?:embre)?|d[ée]c(?:embre)?)\.?\s*(\d{4})?", re.I)


def annee_probable(j, m, a):
    if a:
        a = int(a)
        return a + 2000 if a < 100 else a
    d = dt.date(AUJOURDHUI.year, m, min(j, 28))
    # une date sans année à plus de 6 mois dans le passé est probablement l'an prochain
    return AUJOURDHUI.year + 1 if (AUJOURDHUI - d).days > 180 else AUJOURDHUI.year


def dates_dans(texte):
    """Toutes les dates trouvées dans un texte, dans l'ordre d'apparition."""
    trouvees = []
    for m in RE_DATE_NUM.finditer(texte):
        j, mo = int(m.group(1)), int(m.group(2))
        if 1 <= j <= 31 and 1 <= mo <= 12:
            try:
                trouvees.append((m.start(), dt.date(annee_probable(j, mo, m.group(3)), mo, j)))
            except ValueError:
                pass
    for m in RE_DATE_TXT.finditer(texte):
        mo = MOIS.get(norm(m.group(2)))
        if mo:
            try:
                trouvees.append((m.start(), dt.date(annee_probable(int(m.group(1)), mo, m.group(3)), mo, int(m.group(1)))))
            except ValueError:
                pass
    return [d for _, d in sorted(trouvees)]


# ------------------------------------------------------------------ promos

RE_PRIX = re.compile(r"(\d{1,4})\s*[,.]\s*(\d{2})\s*€|(\d{1,4})\s*€\s*(\d{2})\b|(\d{1,4})\s*€")
RE_REMISE = re.compile(
    r"(-\s?\d{1,2}\s?%(?:\s*(?:de remise|d[ée]duits?|en bons? d'achat|sur le 2e|sur le 3e))?"
    r"|\d\s?(?:e|ème|eme)\s+(?:à|a)\s+-?\s?\d{1,3}\s?%"
    r"|\d\s?(?:e|ème|eme)\s+(?:offert|gratuit)"
    r"|\d\s?\+\s?\d\s+offerts?"
    r"|lot de \d+"
    r"|\d+\s?%\s+(?:offerts?|gratuits?|en plus))", re.I)
MOTS_PAS_NOM = re.compile(r"^(soit|le kg|le litre|prix|ticket|ou|au lieu de|dont|valable|offre|voir|j'en profite|ajouter)", re.I)


def prix_txt(m):
    if m.group(1):
        return float(f"{m.group(1)}.{m.group(2)}")
    if m.group(3):
        return float(f"{m.group(3)}.{m.group(4)}")
    return float(m.group(5))


def fmt_prix(v):
    return f"{v:.2f} €".replace(".", ",")


def produits_jsonld(soup):
    trouves = []

    def visiter(o):
        if isinstance(o, list):
            for x in o:
                visiter(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            t = t if isinstance(t, list) else [t]
            if "Product" in t and o.get("name"):
                offres = o.get("offers") or {}
                offres = offres[0] if isinstance(offres, list) and offres else offres
                prix = offres.get("price") or offres.get("lowPrice") if isinstance(offres, dict) else None
                try:
                    p = float(str(prix).replace(",", "."))
                    trouves.append({"nom": propre(o["name"]), "prix": fmt_prix(p), "prix_avant": "", "remise": ""})
                except (TypeError, ValueError):
                    pass
            for v in o.values():
                visiter(v)

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            visiter(json.loads(s.string or ""))
        except Exception:
            pass
    return trouves


def produits_texte(soup):
    """Repère les blocs « nom + prix » dans la page, quelle que soit sa mise en forme."""
    for t in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
        t.decompose()
    blocs, vus = [], set()
    for noeud in soup.find_all(string=RE_PRIX):
        bloc = noeud.parent
        for _ in range(6):
            texte = propre(bloc.get_text(" "))
            lettres = len(re.sub(r"[^A-Za-zÀ-ÿ]", "", RE_PRIX.sub("", RE_REMISE.sub("", texte))))
            if lettres >= 12 or bloc.parent is None:
                break
            bloc = bloc.parent
        if id(bloc) in vus or len(propre(bloc.get_text(" "))) > 600:
            continue
        vus.add(id(bloc))
        blocs.append(bloc)
    # garder les blocs les plus petits (un bloc qui contient d'autres blocs est une liste)
    ids = {id(b) for b in blocs}
    blocs = [b for b in blocs if not any(id(p) in ids for p in b.find_all(True))]

    produits = []
    for b in blocs:
        lignes = [propre(x) for x in b.get_text("\n").split("\n") if propre(x)]
        texte = " ".join(lignes)
        prix = sorted({prix_txt(m) for m in RE_PRIX.finditer(texte) if 0.05 <= prix_txt(m) <= 2000})
        if not prix:
            continue
        noms = [l for l in lignes if len(re.sub(r"[^A-Za-zÀ-ÿ]", "", l)) >= 4
                and not RE_PRIX.fullmatch(l) and not RE_REMISE.fullmatch(l) and not MOTS_PAS_NOM.match(l)
                and not re.fullmatch(r"[\d\s/.,:-]+", l)]
        if not noms:
            continue
        nom = max(noms, key=len)
        nom = propre(RE_PRIX.sub("", nom)).strip(" -–:")
        if len(nom) < 4 or len(nom) > 160:
            continue
        remise = RE_REMISE.search(texte)
        produits.append({
            "nom": nom,
            "prix": fmt_prix(prix[0]),
            "prix_avant": fmt_prix(prix[-1]) if len(prix) > 1 and prix[-1] > prix[0] * 1.05 else "",
            "remise": propre(remise.group(0)) if remise else "",
        })
    return produits


def remise_calculee(p):
    if not p["prix_avant"] or (p["remise"] and "%" in p["remise"]):
        return p["remise"]
    a = float(p["prix_avant"].replace(" €", "").replace(",", "."))
    n = float(p["prix"].replace(" €", "").replace(",", "."))
    return f"-{round((1 - n / a) * 100)} %" if a > n else ""


CATS = [
    ("surgeles", ["surgele", "surgelee", "glace", "glaces", "cornet", "frites surgelees", "picard"]),
    ("hygiene_maison", ["lessive", "shampooing", "gel douche", "dentifrice", "papier toilette", "essuie tout", "couches",
                        "deodorant", "liquide vaisselle", "nettoyant", "eponge", "mouchoirs", "rasoir", "adoucissant",
                        "javel", "sac poubelle", "pastilles lave", "savon", "coton", "litiere", "croquettes", "patee"]),
    ("boissons", ["vin", "aop", "aoc", "igp", "champagne", "cremant", "biere", "whisky", "rhum", "vodka", "pastis",
                  "cidre", "jus", "soda", "cola", "eau minerale", "eau de source", "eau gazeuse", "sirop", "nectar",
                  "cafe", "the ", "infusion", "chateau", "cuvee", "porto", "ricard", "liqueur", "boisson"]),
    ("viande_poisson", ["poulet", "dinde", "boeuf", "bœuf", "veau", "porc", "agneau", "canard", "jambon", "saucisse",
                        "steak", "hache", "roti", "cote", "filet", "escalope", "lardon", "bacon", "chipolata", "merguez",
                        "saumon", "cabillaud", "colin", "thon", "crevette", "moule", "poisson", "truite", "lieu",
                        "sardine", "maquereau", "volaille", "cuisse", "pintade", "lapin", "saucisson", "pate ", "terrine",
                        "rillettes", "chorizo", "blanc de", "noix de saint", "surimi", "gambas", "andouillette", "boudin"]),
    ("cremerie", ["lait", "yaourt", "fromage", "emmental", "comte", "camembert", "brie", "beurre", "creme fraiche",
                  "creme ", "oeuf", "œuf", "mozzarella", "raclette", "chevre", "coulommiers", "roquefort", "gruyere",
                  "fromage blanc", "petit suisse", "dessert lacte", "skyr", "feta", "parmesan", "reblochon", "mascarpone"]),
    ("fruits_legumes", ["pomme", "poire", "banane", "orange", "clementine", "mandarine", "citron", "raisin", "fraise",
                        "kiwi", "ananas", "melon", "peche", "abricot", "prune", "tomate", "courgette", "carotte",
                        "salade", "laitue", "endive", "poireau", "oignon", "echalote", "ail", "pomme de terre",
                        "patate", "champignon", "poivron", "aubergine", "concombre", "chou", "brocoli", "haricot vert",
                        "epinard", "potiron", "butternut", "courge", "avocat", "radis", "navet", "celeri", "fenouil",
                        "betterave", "mache", "cerise", "framboise", "myrtille", "noix", "chataigne", "legume", "fruit"]),
    ("epicerie", ["pates", "pate ", "spaghetti", "penne", "riz", "farine", "sucre", "huile", "vinaigre", "moutarde",
                  "mayonnaise", "ketchup", "sauce", "conserve", "biscuit", "gateau", "chocolat", "cereales", "confiture",
                  "pain", "brioche", "chips", "compote", "lentille", "pois chiche", "haricot", "semoule", "quinoa",
                  "bouillon", "epice", "sel", "poivre", "miel", "nutella", "pate a tartiner", "madeleine", "galette",
                  "crepe", "pizza", "quiche", "lasagne", "plat cuisine", "soupe", "veloute", "tortilla", "wrap",
                  "olive", "cornichon", "mais", "petits pois", "puree", "gnocchi", "ravioli", "nouilles"]),
]


def singulier(txt):
    """norm() + mots au singulier (courgettes -> courgette, poireaux -> poireau)."""
    return " ".join(w[:-1] if len(w) > 3 and w[-1] in "sx" else w for w in norm(txt).split())


def categorie(nom):
    n = " " + singulier(nom) + " "
    for cat, mots in CATS:
        for m in mots:
            if " " + singulier(m) + " " in n:
                return cat
    return "autre"


def catalogues_magasin(mag):
    """Catalogues en cours d'un magasin : liens nos-catalogues-promos-v2.e.leclerc/catalog/<opération>/<magasin>
    trouvés dans la page e.leclerc du magasin (comme la fonction Supabase de MonLeclercMaVille)."""
    soup, html = lire(mag["url"], f"magasin_{mag['id']}")
    catalogues = {}
    for a in soup.find_all("a", href=True):
        m = RE_LIEN_CATALOGUE.search(a["href"])
        if not m:
            continue
        op, shop = m.group(1), m.group(2)
        libelle = propre(a.get_text(" "))
        img = a.find("img")
        if not libelle and img:
            libelle = propre(img.get("alt", ""))
        if CATALOGUES_EXCLUS.search(libelle):
            continue
        num = f"{op}/{shop}"
        dates = dates_dans(libelle)
        titre = re.sub(r"\s+du\s.+$", "", libelle, flags=re.I).strip()
        c = catalogues.setdefault(num, {"numero": num, "op": op, "shop": shop,
                                        "url": f"{CATALOGUE_SITE}/catalog/{op}/{shop}", "titre": titre, "du": "", "au": ""})
        if len(dates) >= 2 and not c["au"]:
            c["du"], c["au"] = dates[0].isoformat(), dates[1].isoformat()
        if titre and not c["titre"]:
            c["titre"] = titre
    if not catalogues:
        # liens présents ailleurs que dans des <a> (données JSON de la page)
        for m in RE_LIEN_CATALOGUE.finditer(html):
            op, shop = m.group(1), m.group(2)
            num = f"{op}/{shop}"
            catalogues.setdefault(num, {"numero": num, "op": op, "shop": shop,
                                        "url": f"{CATALOGUE_SITE}/catalog/{op}/{shop}", "titre": "", "du": "", "au": ""})
    valides = [c for c in catalogues.values()
               if (not c["au"] or c["au"] >= AUJOURDHUI.isoformat()) and (not c["du"] or c["du"] <= AUJOURDHUI.isoformat())]
    if not valides:
        sauver_debug(f"magasin_{mag['id']}", html)
    return valides


def nombre(v):
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", "."))
    except ValueError:
        return None


def libelle_remise(code):
    """L'API donne un type d'offre (« Prix simple », « TEL % », « -X% sur le Xeme produit »…), pas un texte prêt
    à afficher : on le traduit. La remise en % est recalculée avec l'ancien prix quand il est connu."""
    c = norm(code)
    if not c or c.startswith("prix simple"):
        return ""
    if c.startswith("tel"):
        return "Avantage ticket E.Leclerc"
    if c.startswith("brii"):
        return "Remise immédiate"
    if "offert" in c and re.search(r"\d", code):
        return propre(code)
    if "offert" in c:
        return "Produits offerts"
    if "xeme" in c or "eme produit" in c:
        return "Remise sur le 2e produit ou plus"
    if re.search(r"\bX\b|X%|Y\b", code):
        return ""
    return propre(code)


def produits_catalogue(cat):
    """Produits d'un catalogue, lus directement dans l'API JSON des catalogues e.leclerc."""
    r = SESSION.get(f"{CATALOGUE_API}/{cat['op']}/{cat['shop']}/products", timeout=40, headers={
        "Accept": "application/json, text/plain, */*",
        "Origin": CATALOGUE_SITE,
        "Referer": cat["url"],
    })
    r.raise_for_status()
    if debug_actif:
        sauver_debug(f"catalogue_{cat['op']}_{cat['shop']}", r.text)
    data = r.json()
    if isinstance(data, dict):
        data = data.get("products") or data.get("items") or []
    produits = []
    for p in data if isinstance(data, list) else []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        prix = nombre(p.get("price"))
        if prix is None:
            continue
        avant = nombre(p.get("priceWithoutDiscount"))
        nom = propre(" — ".join(x for x in [p.get("name"), p.get("brand")] if x))
        produits.append({"nom": nom, "prix": fmt_prix(prix),
                         "prix_avant": fmt_prix(avant) if avant and avant > prix else "",
                         "remise": libelle_remise(p.get("discountLabel") or ""),
                         "theme": propre(p.get("thematic") or "")})
    if not produits:
        sauver_debug(f"catalogue_{cat['op']}_{cat['shop']}", r.text)
    return produits


def collecter_promos():
    catalogues, promos = {}, {}
    for mag in MAGASINS:
        try:
            liste = catalogues_magasin(mag)
        except Exception as e:
            avertissements.append(f"Catalogues {mag['nom']} illisibles : {e}")
            continue
        if not liste:
            avertissements.append(f"Aucun catalogue en cours trouvé pour {mag['nom']}")
        for c in liste:
            catalogues.setdefault(c["numero"], {**c, "magasins": []})["magasins"].append(mag["id"])
    return catalogues


def lire_promos(catalogues):
    promos = {}
    for num, cat in catalogues.items():
        try:
            produits = produits_catalogue(cat)
        except Exception as e:
            avertissements.append(f"Catalogue {num} illisible : {e}")
            continue
        for p in produits:
            p["categorie"] = categorie(p["nom"])
            if p["categorie"] == "autre" and p.get("theme"):
                p["categorie"] = categorie(p["theme"])
        # un catalogue presque sans alimentaire (jouets, bricolage...) est ignoré
        alim = [p for p in produits if p["categorie"] != "autre"]
        if produits and len(alim) < 0.3 * len(produits):
            print(f"  catalogue {num} ignoré (non alimentaire)")
            continue
        for p in produits:
            cle = norm(p["nom"]) + "|" + p["prix"]
            if cle in promos:
                promos[cle]["magasins"] = sorted(set(promos[cle]["magasins"]) | set(cat["magasins"]))
                continue
            promos[cle] = {
                "id": "p" + court_id(cle), "nom": p["nom"], "remise": remise_calculee(p), "prix": p["prix"],
                "prix_avant": p["prix_avant"], "categorie": p["categorie"], "magasins": list(cat["magasins"]),
                "du": cat["du"], "au": cat["au"], "catalogue": cat["titre"] or num,
            }
        print(f"  catalogue {num} : {len(produits)} produits")
    ordre = ["viande_poisson", "fruits_legumes", "cremerie", "epicerie", "surgeles", "boissons", "hygiene_maison", "autre"]
    return sorted(promos.values(), key=lambda p: (ordre.index(p["categorie"]), p["nom"]))


# ------------------------------------------------------------------ recettes

# Recettes « simples » : rapides, peu d'ingrédients, peu d'étapes
RECETTE_TEMPS_MAX = 45
RECETTE_INGREDIENTS_MAX = 6
RECETTE_ETAPES_MAX = 5
# produits en promo à ne jamais prendre comme ingrédient (plats tout prêts, desserts, animaux…)
PAS_INGREDIENT = re.compile(r"\b(dessert|chocolat|snack|menu|patee|croquette|chat|chien|tous mes amis|biscuit|barre|"
                            r"compote|riz au lait|pasta salade|danonino|yaourt aux fruits|muffin|sandwich|croque|pizz|"
                            r"farci|ravioli|cups|nouilles|tartiner|creme dessert|entremets)", re.I)

UNITES = [  # (motif, unité du tableau de prix, facteur)
    (r"kg", "kg", 1), (r"g", "kg", 0.001), (r"l", "l", 1), (r"cl", "l", 0.01), (r"ml", "l", 0.001),
    (r"gousses?", "gousse", 1), (r"tranches?", "tranche", 1), (r"c\. ?a soupe", "cas", 1), (r"c\. ?a cafe", "cac", 1),
    (r"pots?", "pot", 1), (r"boites?", "boite", 1), (r"bocal", "bocal", 1), (r"bouquet", "bouquet", 1),
    (r"pincee", "pincee", 1), (r"grappe", "grappe", 1),
]


def quantite_unite(txt):
    """'600 g' -> (0.6, 'kg') ; '1/2 pot' -> (0.5, 'pot') ; '3' -> (3, 'piece')."""
    t = norm(txt).replace(" 2 ", " ")
    brut = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode().lower()
    m = re.match(r"\s*(\d+)\s*/\s*(\d+)\s*(.*)", brut) or re.match(r"\s*(\d+(?:[.,]\d+)?)\s*(.*)", brut)
    if not m:
        return 1.0, "piece"
    if len(m.groups()) == 3:
        q, reste = int(m.group(1)) / int(m.group(2)), m.group(3)
    else:
        q, reste = float(m.group(1).replace(",", ".")), m.group(2)
    reste = reste.strip()
    for motif, unite, f in UNITES:
        if re.fullmatch(motif, reste):
            return q * f, unite
    return q, "piece"


def prix_normal(nom, quantite, table):
    """Prix hors promo de la quantité utilisée, ou None si inconnu."""
    prix = table.get(nom, ({}, ""))[0]
    if not prix:
        return None
    q, unite = quantite_unite(quantite)
    if unite in prix:
        return q * prix[unite]
    if unite == "piece" and len(prix) == 1:  # « 1 mozzarella », « 1 boîte »…
        return q * next(iter(prix.values()))
    return None


def coef_promo(p):
    """Prix promo / prix normal pour la promo, si on peut le savoir."""
    try:
        if p.get("prix_avant"):
            a = float(p["prix_avant"].replace(" €", "").replace(",", "."))
            n = float(p["prix"].replace(" €", "").replace(",", "."))
            if a > n > 0:
                return n / a
    except ValueError:
        pass
    m = re.search(r"-\s?(\d{1,2})\s?%", p.get("remise") or "")
    return 1 - int(m.group(1)) / 100 if m else 1.0


OPEN_PRICES_API = "https://prices.openfoodfacts.org/api/v1/prices"
OPEN_PRICES_CACHE = RACINE / "data" / "prix_open_prices.json"
OPEN_PRICES_JOURS = 7  # on ne réinterroge Open Prices qu'une fois par semaine
OPEN_PRICES_VERSION = 2  # à augmenter quand le calcul change, pour forcer une mise à jour


def mediane(v):
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def prix_open_prices_tag(tag):
    """Prix médian non promo en France pour une catégorie Open Food Facts : {'kg': x} / {'l': x} / {'piece': x}."""
    depuis = (AUJOURDHUI - dt.timedelta(days=548)).isoformat()
    vus = []
    for params in ({"category_tag": tag}, {"product__categories_tags__contains": tag}):
        r = SESSION.get(OPEN_PRICES_API, timeout=30, params={**params, "currency": "EUR", "date__gte": depuis,
                                                             "order_by": "-date", "size": 100})
        r.raise_for_status()
        vus += r.json().get("items", [])
    par = {"kg": [], "l": [], "piece": []}
    leclerc = {"kg": [], "l": [], "piece": []}
    # produit frais (prix au kg relevé en rayon) : on ignore les produits emballés de la même catégorie
    # (« oignons frits », « jus de citron »…)
    frais = sum(1 for it in vus if it.get("type") == "CATEGORY" and it.get("price_per") == "KILOGRAM") >= 3
    for it in vus:
        if frais and it.get("type") != "CATEGORY":
            continue
        loc = it.get("location") or {}
        if loc.get("osm_address_country_code") not in (None, "FR") or it.get("price_is_discounted"):
            continue
        prix = it.get("price")
        if not isinstance(prix, (int, float)) or prix <= 0:
            continue
        unite, val = None, None
        if it.get("type") == "CATEGORY":
            if it.get("price_per") == "KILOGRAM":
                unite, val = "kg", prix
            elif it.get("price_per") == "UNIT" and not frais:
                unite, val = "piece", prix
        else:
            prod = it.get("product") or {}
            q, u = prod.get("product_quantity"), (prod.get("product_quantity_unit") or "").lower()
            if isinstance(q, (int, float)) and q > 0:
                if u == "g":
                    unite, val = "kg", prix / (q / 1000)
                elif u == "ml":
                    unite, val = "l", prix / (q / 1000)
        if unite:
            par[unite].append(val)
            if "leclerc" in norm(loc.get("osm_brand") or loc.get("osm_name") or ""):
                leclerc[unite].append(val)
    res = {}
    for unite in par:
        source = leclerc[unite] if len(leclerc[unite]) >= 3 else par[unite]
        if len(source) >= 3:
            res[unite] = round(mediane(source), 2)
    return res


def prix_open_prices(config):
    """Prix Open Prices par ingrédient, gardés en cache une semaine dans data/prix_open_prices.json."""
    cache = {}
    if OPEN_PRICES_CACHE.exists():
        cache = json.loads(OPEN_PRICES_CACHE.read_text(encoding="utf-8"))
        if (cache.get("version") == OPEN_PRICES_VERSION
                and cache.get("date", "") >= (AUJOURDHUI - dt.timedelta(days=OPEN_PRICES_JOURS)).isoformat()):
            return cache.get("prix", {})
    print("Open Prices…")
    par_tag, erreurs = {}, 0
    for c in config.values():
        tag = c["tag"]
        if tag in par_tag:
            continue
        try:
            par_tag[tag] = prix_open_prices_tag(tag)
        except Exception as e:
            erreurs += 1
            par_tag[tag] = None
            if erreurs == 3:
                avertissements.append(f"Open Prices injoignable ({e}) : prix estimés utilisés")
                return cache.get("prix", {})
    prix = {}
    for nom, c in config.items():
        trouve = par_tag.get(c["tag"])
        if not trouve:
            continue
        p = dict(trouve)
        if "kg" in p and c.get("poids_piece_kg"):
            p["piece"] = round(p["kg"] * c["poids_piece_kg"], 2)
        prix[nom] = p
    print(f"  {len(prix)} ingrédients avec un prix Open Prices")
    OPEN_PRICES_CACHE.parent.mkdir(exist_ok=True)
    OPEN_PRICES_CACHE.write_text(json.dumps({"version": OPEN_PRICES_VERSION, "date": AUJOURDHUI.isoformat(), "prix": prix}, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    return prix


def table_des_prix():
    """Prix normaux par ingrédient + d'où vient chaque prix : mes_prix > Open Prices > estimation."""
    ref = json.loads((RACINE / "scripts" / "prix_reference.json").read_text(encoding="utf-8"))
    config = ref.get("open_prices", {})
    # table[nom] = (prix par unité, source par unité)
    table = {nom: (dict(p), {u: "estimation" for u in p}) for nom, p in ref["prix"].items()}
    for nom, p in prix_open_prices(config).items():
        estim = dict(table.get(nom, ({}, {}))[0])
        poids = config.get(nom, {}).get("poids_piece_kg")
        if "kg" not in estim and "piece" in estim and poids:
            estim["kg"] = estim["piece"] / poids
        # garde-fou : un prix Open Prices très loin de l'estimation est sans doute un autre produit
        for u, v in p.items():
            if u in estim and 0.5 * estim[u] <= v <= 2.0 * estim[u]:
                table[nom][0][u] = v
                table[nom][1][u] = "open_prices"
    for nom, p in (ref.get("mes_prix") or {}).items():
        prix, src = table.get(nom, ({}, {}))
        table[nom] = ({**prix, **p}, {**src, **{u: "mes_prix" for u in p}})
    return table


def source_prix(nom, quantite, table):
    prix, src = table.get(nom, ({}, {}))
    q, unite = quantite_unite(quantite)
    if unite in src:
        return src[unite]
    return next(iter(src.values()), "estimation") if len(prix) == 1 else "estimation"


def tete_produit(nom):
    """Début du nom du produit, sans la marque ni le nombre : « 8 saucisses de toulouse — X » -> « saucisse de toulouse »."""
    t = singulier(nom.split(" — ")[0])
    return re.sub(r"^(lot de |x ?)?\d+( x)? ", "", t)


NOTE_PRIX = ("Prix de revient approximatif : les ingrédients hors promo sont estimés (moyennes Open Prices "
             "ou estimations), sauf ceux relevés sur vos tickets de caisse.")


def choisir_recettes(promos, n=10):
    base = json.loads((RACINE / "scripts" / "recettes_base.json").read_text(encoding="utf-8"))
    table = table_des_prix()
    base = [r for r in base if r["temps_min"] <= RECETTE_TEMPS_MAX and len(r["ingredients"]) <= RECETTE_INGREDIENTS_MAX
            and len(r["etapes"]) <= RECETTE_ETAPES_MAX]
    cuisine = [p for p in promos if p["categorie"] in CATEGORIES_CUISINE and not PAS_INGREDIENT.search(norm(p["nom"]))]
    index = [(" " + tete_produit(p["nom"]) + " ", p) for p in cuisine]
    candidates = []
    for r in base:
        ingredients, utilises = [], set()
        total = total_sans_promo = 0.0
        complet = True
        for ing in r["ingredients"]:
            promo = None
            for mot in ing.get("mots", []):
                cle = " " + singulier(mot) + " "
                # le produit doit COMMENCER par l'ingrédient (« Oignons jaunes » oui, « Boudin aux oignons » non)
                promo = next((p for n_, p in index if n_.startswith(cle) and p["id"] not in utilises
                              and not (" pomme de terre" in n_ and "terre" not in cle)), None)
                if promo:
                    break
            normal = prix_normal(ing["nom"], ing["quantite"], table)
            if normal is None:
                complet = False
                cout = 0.0
            else:
                cout = normal * (coef_promo(promo) if promo else 1.0)
                total += cout
                total_sans_promo += normal
            if promo:
                utilises.add(promo["id"])
            source = "promo" if promo else source_prix(ing["nom"], ing["quantite"], table)
            ingredients.append({"nom": ing["nom"], "quantite": ing["quantite"], "promo_id": promo["id"] if promo else "",
                                "en_promo": bool(promo), "prix": fmt_prix(cout) if normal is not None else "",
                                "prix_approximatif": source != "mes_prix",
                                "prix_source": source})
        nb = len(utilises)
        if nb and complet:
            candidates.append((nb, total_sans_promo - total, total, r, ingredients))
    # d'abord les recettes avec le plus d'ingrédients en promo, puis la plus grosse économie
    candidates.sort(key=lambda x: (-x[0], -x[1], x[3]["titre"]))
    choix, deja = [], {}
    for nb, eco, total, r, ings in candidates:
        principal = next(i["promo_id"] for i in ings if i["promo_id"])
        if deja.get(principal, 0) >= 2:  # variété : pas 5 recettes avec le même produit
            continue
        deja[principal] = deja.get(principal, 0) + 1
        choix.append({"id": "r" + court_id(r["titre"]), "titre": r["titre"], "temps_min": r["temps_min"],
                      "personnes": r["personnes"], "nb_promos": nb,
                      "prix_revient": fmt_prix(total), "prix_par_personne": fmt_prix(total / r["personnes"]),
                      "economie": fmt_prix(eco) if eco >= 0.05 else "",
                      "prix_approximatif": any(i["prix_approximatif"] for i in ings),
                      "note_prix": NOTE_PRIX,
                      "ingredients": ings, "etapes": r["etapes"]})
        if len(choix) >= n:
            break
    return choix


# ------------------------------------------------------------------ ma ville

MOTS_ALERTE = ["travaux", "fermeture", "ferme", "coupure", "alerte", "vigilance", "risque", "interdiction", "interdit",
               "deviation", "inondation", "canicule", "orage", "tempete", "neige", "verglas", "accident",
               "perturbation", "stationnement", "eau potable", "secheresse"]


def est_alerte(texte):
    t = " " + norm(texte) + " "
    return any(" " + m in t for m in MOTS_ALERTE)


def panneaupocket():
    url = SOURCES_VILLE["panneaupocket"]
    soup, html = lire(url, "panneaupocket")
    marque = re.compile(r"Info (?:publi[ée]e|modifi[ée]e) le\s*(\d{2}/\d{2}/\d{4})", re.I)
    items, vus = [], set()
    for noeud in soup.find_all(string=marque):
        bloc = noeud.parent
        while bloc.parent is not None and len(bloc.parent.find_all(string=marque)) == 1:
            bloc = bloc.parent
        if id(bloc) in vus:
            continue
        vus.add(id(bloc))
        date = dates_dans(marque.search(noeud).group(1))
        lien = bloc.find("a", href=re.compile(r"panneau=\d+"))
        titre_el = bloc.find(["h1", "h2", "h3", "h4", "h5", "strong", "b"])
        lignes = [propre(x) for x in bloc.get_text("\n").split("\n") if propre(x)]
        lignes = [l for l in lignes if not marque.search(l) and not re.fullmatch(r"(Le Houlme|76770|\d{5})", l)
                  and norm(l) not in ("voir", "lire la suite", "en savoir plus", "partager", "imprimer")]
        titre = propre(titre_el.get_text(" ")) if titre_el else (lignes[0] if lignes else "")
        corps = propre(" ".join(l for l in lignes if l != titre))
        if not titre or "infos pratiques mairie" in norm(titre):
            continue
        items.append({"titre": titre, "date": date[0].isoformat() if date else "", "texte": corps,
                      "url": urljoin(url, lien["href"]) if lien else url})
    if not items:
        sauver_debug("panneaupocket", html)
        avertissements.append("PanneauPocket : aucune info lue")
    return items


def resume(texte, n=180):
    texte = propre(texte)
    if len(texte) <= n:
        return texte
    coupe = texte[:n].rsplit(" ", 1)[0]
    return coupe + "…"


def site_mairie(cle, motif_lien):
    url = SOURCES_VILLE[cle]
    soup, html = lire(url, f"mairie_{cle}")
    for t in soup(["script", "style", "nav", "footer", "header"]):
        t.decompose()
    items, vus = [], set()
    for a in soup.find_all("a", href=re.compile(motif_lien)):
        lien = urljoin(url, a["href"])
        if lien in vus or lien.rstrip("/") == url.rstrip("/"):
            continue
        bloc = a
        for _ in range(5):
            if bloc.parent is None:
                break
            autres = {urljoin(url, x["href"]) for x in bloc.parent.find_all("a", href=re.compile(motif_lien))}
            if len(autres - {lien}) > 0:  # le parent contient déjà un autre article
                break
            bloc = bloc.parent
        titre = propre(a.get("title") or a.get_text(" "))
        if len(titre) < 4 or norm(titre) in ("lire la suite", "en savoir plus", "voir", "plus"):
            h = bloc.find(["h2", "h3", "h4"])
            titre = propre(h.get_text(" ")) if h else titre
        if len(titre) < 4:
            continue
        vus.add(lien)
        texte = propre(bloc.get_text(" "))
        dates = dates_dans(texte)
        heure = re.search(r"\b(\d{1,2})\s?h\s?(\d{2})?\b", texte)
        items.append({"titre": titre, "texte": texte.replace(titre, "", 1).strip(), "url": lien,
                      "dates": dates, "heure": f"{heure.group(1)}h{heure.group(2) or ''}" if heure else ""})
    if not items:
        sauver_debug(f"mairie_{cle}", html)
        avertissements.append(f"le-houlme.fr ({cle}) : rien lu")
    return items


def collecter_ville():
    limite_actus = (AUJOURDHUI - dt.timedelta(days=45)).isoformat()
    limite_alertes = (AUJOURDHUI - dt.timedelta(days=90)).isoformat()
    alertes, actus, agenda, ok = [], [], [], 0

    try:
        for it in panneaupocket():
            ok += 1
            if est_alerte(it["titre"]) and (not it["date"] or it["date"] >= limite_alertes):
                alertes.append({"titre": it["titre"], "texte": resume(it["texte"], 300), "source": "PanneauPocket", "url": it["url"]})
            elif not it["date"] or it["date"] >= limite_actus:
                actus.append({"titre": it["titre"], "date": it["date"], "resume": resume(it["texte"]),
                              "source": "PanneauPocket", "url": it["url"]})
    except Exception as e:
        avertissements.append(f"PanneauPocket inaccessible : {e}")

    try:
        for it in site_mairie("actus", r"/actualite/\d+"):
            ok += 1
            d = it["dates"][0].isoformat() if it["dates"] else ""
            if d and d < limite_actus:
                continue
            actus.append({"titre": it["titre"], "date": d, "resume": resume(it["texte"]), "source": "le-houlme.fr", "url": it["url"]})
    except Exception as e:
        avertissements.append(f"Actualités le-houlme.fr inaccessibles : {e}")

    try:
        for it in site_mairie("agenda", r"/(agenda|evenement)[^\"']*\d+"):
            ok += 1
            futures = [d for d in it["dates"] if d >= AUJOURDHUI]
            if not futures:
                continue
            lieu = re.search(r"(?:Lieu|Adresse)\s*:?\s*([^|•]{3,60})", it["texte"])
            agenda.append({"titre": it["titre"], "date": futures[0].isoformat(), "heure": it["heure"],
                           "lieu": propre(lieu.group(1)) if lieu else "", "source": "le-houlme.fr", "url": it["url"]})
    except Exception as e:
        avertissements.append(f"Agenda le-houlme.fr inaccessible : {e}")

    def dedoublonner(liste):
        vus, garde = set(), []
        for x in liste:
            k = norm(x["titre"])[:35]
            if k not in vus:
                vus.add(k)
                garde.append(x)
        return garde

    if not ok:
        return None
    return {
        "commune": "Le Houlme",
        "alertes": dedoublonner(alertes),
        "actus": sorted(dedoublonner(actus), key=lambda a: a["date"] or "0", reverse=True)[:25],
        "agenda": sorted(dedoublonner(agenda), key=lambda e: e["date"])[:25],
        "pratique": PRATIQUE,
    }


# ------------------------------------------------------------------ main

def main():
    ancien = json.loads(SORTIE.read_text(encoding="utf-8")) if SORTIE.exists() else {}
    data = {
        "version": 1,
        "genere_le": MAINTENANT.strftime("%Y-%m-%dT%H:%M"),
        "promos_maj_le": ancien.get("promos_maj_le", ""),
        "magasins": [{"id": m["id"], "nom": m["nom"]} for m in MAGASINS],
        "catalogues_vus": ancien.get("catalogues_vus", []),
        "promos": ancien.get("promos", []),
        "recettes": ancien.get("recettes", []),
        "ville": ancien.get("ville", {}),
    }

    print("Promos…")
    catalogues = collecter_promos()
    vus = sorted([{"numero": c["numero"], "magasins": sorted(c["magasins"]), "du": c["du"], "au": c["au"]}
                  for c in catalogues.values()], key=lambda c: c["numero"])
    anciens_nums = sorted(c.get("numero", "") for c in data["catalogues_vus"])
    # promos enregistrées avec les codes bruts de l'API (ancienne version du script) : on les relit
    codes_bruts = any(libelle_remise(p.get("remise", "")) != p.get("remise", "") for p in data["promos"])
    if catalogues and ([c["numero"] for c in vus] != anciens_nums or not data["promos"] or debug_actif or codes_bruts):
        promos = lire_promos(catalogues)
        if promos:
            data["promos"], data["catalogues_vus"] = promos, vus
            data["promos_maj_le"] = data["genere_le"]
            data["recettes"] = choisir_recettes(promos) or data["recettes"]
        else:
            avertissements.append("Aucun produit lu dans les catalogues : promos de la fois précédente conservées")
    else:
        print("  catalogues inchangés")
    # retirer les promos terminées
    data["promos"] = [p for p in data["promos"] if not p.get("au") or p["au"] >= AUJOURDHUI.isoformat()]
    data["recettes"] = choisir_recettes(data["promos"]) if data["promos"] else data["recettes"]

    print("Ma ville…")
    ville = collecter_ville()
    if ville:
        data["ville"] = ville
    data["avertissements"] = avertissements

    def sans_horodatage(d):
        return {k: v for k, v in d.items() if k != "genere_le"}

    if ancien and sans_horodatage(ancien) == sans_horodatage(data):
        print("Rien de nouveau.")
        return
    SORTIE.parent.mkdir(exist_ok=True)
    SORTIE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Écrit : {len(data['promos'])} promos, {len(data['recettes'])} recettes, "
          f"{len(data['ville'].get('actus', []))} actus, {len(data['ville'].get('agenda', []))} événements, "
          f"{len(data['ville'].get('alertes', []))} alertes")
    for a in avertissements:
        print("  ! " + a)


if __name__ == "__main__":
    sys.exit(main())
