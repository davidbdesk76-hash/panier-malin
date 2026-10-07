"""
Panier Malin - collecte automatique (GitHub Actions, sans IA)

- Promos de l'E.Leclerc du Houlme (catalogues e.leclerc : page du magasin + API des catalogues)
- Promos du Lidl (catalogues de la semaine lidl.fr : texte du PDF du catalogue)
- Affaires de la semaine Action (action.com, les mêmes dans tous les magasins)
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
    {"id": "houlme", "nom": "Leclerc", "url": "https://www.e.leclerc/mag/e-leclerc-le-houlme"},
]
LIDL = {"id": "lidl", "nom": "Lidl"}
LIDL_CATALOGUES_URL = "https://www.lidl.fr/c/catalogues-en-ligne/s10017753"
LIDL_LEAFLET_API = "https://endpoints.leaflets.schwarz/v4/flyer"
RE_LIDL_LEAFLET = re.compile(r"/l/catalogue-de-la-semaine/([a-z0-9-]+)/")
LIDL_VERSION = 3  # à augmenter quand la lecture des catalogues Lidl change : ils sont alors relus
LIDL_JOURS_AVANCE = 2  # catalogues qui commencent dans les 2 prochains jours : inclus (« à partir de jeudi »)
ACTION = {"id": "action", "nom": "Action"}
ACTION_URL = "https://www.action.com/fr-fr/les-affaires-du-moment/"
ACTION_PAGES_MAX = 12
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
    txt = txt or ""
    if "Ã" in txt or "â€" in txt:  # texte UTF-8 mal décodé : on le répare
        try:
            txt = txt.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return re.sub(r"\s+", " ", txt).strip()


def texte_html(html, n=1200):
    """Texte lisible depuis un bout de HTML (descriptions produits)."""
    if not html:
        return ""
    soup = BeautifulSoup(str(html), "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for bloc in soup.find_all(["p", "li", "div", "h1", "h2", "h3", "h4", "tr"]):
        bloc.append("\n")
    t = soup.get_text("")
    lignes = [propre(x) for x in t.split("\n") if propre(x)]
    t = "\n".join(lignes)
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0] + "…"


def court_id(*parts):
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:8]


def lire(url, nom_debug):
    r = SESSION.get(url, timeout=40)
    r.raise_for_status()
    # sans « charset » dans l'en-tête, requests lit la page en latin-1 (« rentrÃ©e ») : les sites lus sont en UTF-8
    if "charset" not in (r.headers.get("Content-Type") or "").lower():
        r.encoding = "utf-8"
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


# Rayons, testés dans cet ordre (le premier qui correspond gagne) : les non-alimentaires et les produits
# « transformés » d'abord, pour que « chocolat au lait » ne parte pas en crèmerie ni « moule à muffins » en poissonnerie.
def euros(txt):
    return float(txt.replace(",", "."))


RE_PAR_LOT = re.compile(r"Par (\d+)[^:]{0,25}:\s*(\d+[,.]\d{2})\s*€\s*au lieu de\s*(\d+[,.]\d{2})\s*€", re.I)
RE_PAR_TICKET = re.compile(r"Par (\d+)[^:]{0,25}:\s*(\d+[,.]\d{2})\s*€\s*avec\s*(\d+[,.]\d{2})\s*€\s*en\s*Ticket", re.I)
RE_TICKET_PCT = re.compile(r"Ticket E\.?\s?Leclerc de (\d{1,2})\s?%", re.I)
RE_TICKET_PRIX = re.compile(r"au prix de (\d+[,.]\d{2})\s?€\s*avec un Ticket E\.?\s?Leclerc de (\d+[,.]\d{2})\s?€", re.I)
TICKET_EXPLIC = "Le montant est crédité sur votre carte E.Leclerc, à utiliser lors d'un prochain passage en magasin."


def detail_remise(remise, description, prix, prix_avant, brut=None):
    """(libellé court, explication, % d'économie réelle) à partir du type d'offre.
    1) champs de l'API (discountAmount, discountUnit, discountPriceQty, priceWithTEL) ;
    2) sinon le texte : « Par 2 (400 g) : 3,85 € au lieu de 5,50 € », « … avec 3,25 € en Ticket E.Leclerc »."""
    b = brut or {}
    montant = nombre(b.get("discountAmount"))
    unite = (b.get("discountUnit") or "").upper()
    qte = int(nombre(b.get("discountPriceQty")) or 0)
    prix_u = nombre(b.get("price")) or 0
    if montant and remise == "Ticket E.Leclerc":
        avec_tel = nombre(b.get("priceWithTEL"))
        par = f" par {qte}" if qte >= 2 else ""
        if unite == "PERCENT":
            pct = round(montant)
            gain = f", soit {fmt_prix(prix_u - avec_tel)} : le produit vous revient à {fmt_prix(avec_tel)}" \
                if avec_tel and prix_u > avec_tel else ""
            return (f"Ticket -{pct} %{par}", f"{pct} % du prix crédités en Ticket E.Leclerc{par}{gain}. " + TICKET_EXPLIC, pct)
        pct = round(montant / prix_u * 100) if prix_u else 0
        return (f"Ticket {fmt_prix(montant)}{par}", f"{fmt_prix(montant)} crédités en Ticket E.Leclerc{par}. " + TICKET_EXPLIC, pct)
    if montant and remise == "2e produit remisé" and unite == "PERCENT":
        n = qte if qte >= 2 else 2
        pct_n = round(montant)
        pct = round(pct_n / n)
        court = f"{n}e offert" if pct_n >= 100 else f"{n}e à -{pct_n} %"
        expl = f"Le {n}e produit identique à -{pct_n} %"
        if prix_u:
            total = prix_u * n - prix_u * montant / 100
            expl += f" : {n} pour {fmt_prix(total)} au lieu de {fmt_prix(prix_u * n)}"
        return (court, expl + f" (soit -{pct} % sur le lot). Avantage appliqué en caisse.", pct)
    d = description or ""
    m = RE_PAR_TICKET.search(d)
    if m:
        n, total, ticket = int(m.group(1)), euros(m.group(2)), euros(m.group(3))
        pct = round(ticket / total * 100) if total else 0
        return (f"Ticket {fmt_prix(ticket)} par {n}",
                f"Par {n} : {fmt_prix(total)}, dont {fmt_prix(ticket)} crédités en Ticket E.Leclerc (soit -{pct} %). "
                + TICKET_EXPLIC, pct)
    m = RE_PAR_LOT.search(d)
    if m:
        n, total, avant = int(m.group(1)), euros(m.group(2)), euros(m.group(3))
        eco = avant - total
        pct = round(eco / avant * 100) if avant else 0
        if n == 2 and avant:
            pct2 = round(eco / (avant / 2) * 100)
            court = "2e offert" if pct2 >= 99 else f"2e à -{pct2} %"
        else:
            court = f"-{pct} % par {n}"
        return (court, f"Par {n} : {fmt_prix(total)} au lieu de {fmt_prix(avant)}, "
                       f"soit {fmt_prix(eco)} d'économie (-{pct} % sur le lot).", pct)
    m = RE_TICKET_PRIX.search(d)
    if m and remise == "Ticket E.Leclerc":
        pct = round(euros(m.group(2)) / euros(m.group(1)) * 100)
        return (f"Ticket -{pct} %", f"{pct} % du prix crédités en Ticket E.Leclerc. " + TICKET_EXPLIC, pct)
    m = RE_TICKET_PCT.search(d)
    if m and remise == "Ticket E.Leclerc":
        pct = int(m.group(1))
        return (f"Ticket -{pct} %", f"{pct} % du prix crédités en Ticket E.Leclerc. " + TICKET_EXPLIC, pct)
    pct = 0
    mm = re.search(r"-\s?(\d{1,2})\s?%", remise or "")
    if mm:
        pct = int(mm.group(1))
    if remise == "Ticket E.Leclerc":
        return (remise, "Une partie du prix est créditée sur votre carte E.Leclerc (montant indiqué en magasin), "
                        "à utiliser lors d'un prochain passage.", 0)
    if remise == "2e produit remisé":
        return (remise, "Remise sur le 2e produit acheté ; le montant est indiqué en magasin.", 0)
    if pct and prix_avant:
        return (remise, f"{prix} au lieu de {prix_avant} (-{pct} %).", pct)
    if remise == "Remise immédiate":
        return (remise, "Remise déduite directement en caisse.", pct)
    return (remise, "", pct)


CATS = [
    ("animaux", ["chat", "chats", "chien", "chiens", "croquette", "litiere", "patee", "griffoir", "arbre a chat",
                 "tous mes ami", "purina", "whiskas", "sheba", "pedigree", "friskies", "felix", "gourmet", "vitakraft",
                 "ultima", "edgar cooper", "lily s kitchen", "dentalife", "boule de graisse", "animaux", "oiseau"]),
    ("maison", ["poele", "casserole", "cocotte ronde", "cocotte en fonte", "wok", "crepiere", "faitout", "autocuiseur", "friteuse", "micro onde",
                "mixeur", "multicuiseur", "cookeo", "moule a", "muffin", "boite", "assiette", "tasse", "bol a",
                "gourde isotherme", "poubelle", "balai", "serviette", "set amovible", "meuble", "tv", "pc portable",
                "smartphone", "telephone", "console", "nintendo", "switch", "casque", "imprimante", "cable", "usb",
                "carte memoire", "montre", "barre de son", "passerelle", "jeu", "jeux", "playmobil", "gravitrax",
                "figurine", "pat patrouille", "pyjama", "boxer", "soutien gorge", "collant", "slip", "sweat", "veste",
                "blouson", "doudoune", "botte", "bottillon", "legging", "t shirt", "tee shirt", "robe", "culotte",
                "brassiere", "debardeur", "pantalon", "rosier", "bouquet de", "adblue", "auto", "ampoule",
                "refroidissement", "echec", "ace combat", "star war", "morgan", "maestro"]),
    ("hygiene_maison", ["lessive", "shampooing", "douche", "dentifrice", "papier toilette", "essuie tout", "couches",
                        "change bebe", "deodorant", "liquide vaisselle", "lave vaisselle", "nettoyant", "eponge",
                        "mouchoir", "rasoir", "adoucissant", "assouplissant", "javel", "sac poubelle", "savon", "coton",
                        "wc", "eau de toilette", "parfum", "creme solaire", "maison net"]),
    ("surgeles", ["surgele", "surgelee", "glace", "glaces", "glace", "cone glace", "baton glace", "batonnet glace",
                  "cornet", "picard"]),
    ("sucre", ["chocolat", "biscuit", "sable", "barre", "tablette", "bonbon", "crepe", "gateau", "madeleine",
               "pain au chocolat", "brioche", "macaron", "napolitain", "chewing gum", "rocher", "pate a tartiner",
               "confiture", "miel", "nutella", "cereale", "compote", "gourde", "entremet", "flan", "creme dessert",
               "riz au lait", "dessert", "danonino", "gache", "viennois", "grainea", "pepite", "cookie", "gaufre"]),
    ("boissons", ["vin", "aop", "aoc", "igp", "doc", "docg", "champagne", "cremant", "prosecco", "moscato", "spumante",
                  "biere", "whisky", "rhum", "vodka", "pastis", "aperol", "amer", "zubrowka", "cidre", "jus", "soda",
                  "cola", "eau minerale", "eau de source", "eau gazeuse", "sirop", "nectar", "cafe", "capsule",
                  "espresso", "the", "infusion", "chateau", "cuvee", "porto", "ricard", "liqueur", "boisson", "rouge",
                  "blanc sec", "rose", "mouton cadet", "tokaji", "cacolac"]),
    ("traiteur", ["croque", "pizza", "marguerite", "tortellini", "pate a tarte", "pate feuilletee", "pate brisee", "pate sablee", "pizz", "quiche", "lasagne", "plat cuisine", "pasta salade", "salade en conserve",
                  "tomate farcie", "ravioli", "houmous", "panier feuillete", "taboule", "sandwich", "wrap", "nem",
                  "soupe", "veloute", "tranches vege", "tranche vege", "repas plaisir", "rio mare", "nouilles"]),
    ("viande_poisson", ["poulet", "dinde", "boeuf", "bœuf", "bovine", "viande", "bourguignon", "veau", "porc", "agneau",
                        "canard", "jambon", "saucisse", "steak", "hache", "roti", "cote", "filet", "escalope", "lardon",
                        "bacon", "chipolata", "merguez", "saumon", "cabillaud", "colin", "thon", "crevette", "moule",
                        "poisson", "truite", "lieu", "sardine", "maquereau", "volaille", "cuisse", "pintade", "lapin",
                        "saucisson", "pate de campagne", "terrine", "rillette", "chorizo", "speck", "involtini",
                        "blanc de", "noix de saint", "surimi", "gambas", "andouillette", "boudin", "dorade", "morue",
                        "cordon bleu"]),
    ("cremerie", ["lait", "yaourt", "fromage", "emmental", "comte", "camembert", "brie", "beurre", "creme fraiche",
                  "creme", "oeuf", "œuf", "mozzarella", "raclette", "chevre", "coulommiers", "roquefort", "gruyere",
                  "fromage blanc", "petit suisse", "suisse", "skyr", "feta", "parmesan", "reblochon", "mascarpone",
                  "leerdammer", "tartine et cuisson", "extra tendre", "st diery", "saint diery"]),
    ("fruits_legumes", ["pomme", "poire", "banane", "orange", "clementine", "mandarine", "citron", "raisin", "fraise",
                        "kiwi", "ananas", "melon", "peche", "abricot", "prune", "tomate", "courgette", "carotte",
                        "salade", "laitue", "endive", "poireau", "oignon", "echalote", "ail", "pomme de terre",
                        "patate", "champignon", "poivron", "aubergine", "concombre", "chou", "brocoli", "haricot vert",
                        "epinard", "potiron", "butternut", "courge", "avocat", "radis", "navet", "celeri", "fenouil",
                        "betterave", "mache", "cerise", "framboise", "myrtille", "chataigne", "legume", "fruit"]),
    ("epicerie", ["pates", "pate", "spaghetti", "penne", "marguerite", "riz", "farine", "sucre", "huile", "vinaigre",
                  "moutarde", "mayonnaise", "ketchup", "sauce", "conserve", "cereales", "pain", "chips", "doritos",
                  "lentille", "pois chiche", "haricot", "semoule", "couscous", "quinoa", "bouillon", "epice", "sel",
                  "poivre", "galette", "tortilla", "olive", "cornichon", "mais", "petits pois", "puree", "gnocchi",
                  "pesto", "amande", "noix", "biscotte", "shirataki", "larnaudie", "snack", "aperitif"]),
]

# Rayon donné par Leclerc (« thematic » de l'API), utilisé en premier quand il est clair
THEMES = [
    ("animaux", r"animal|animaux"),
    ("maison", r"textile|mode|vetement|lingerie|high tech|multimedia|electromenager|bazar|cuisine et maison|jouet|"
               r"jeux|auto|jardin|bricolage|culture|maison"),
    ("hygiene_maison", r"hygiene|beaute|entretien|droguerie|parfumerie|bebe"),
    ("surgeles", r"surgele"),
    ("boissons", r"vin|boisson|alcool|spiritueux|cave|biere"),
    ("traiteur", r"traiteur"),
    ("viande_poisson", r"boucherie|volaille|poissonnerie|maree|charcuterie"),
    ("cremerie", r"cremerie|fromage|produits laitiers|ultra frais"),
    ("fruits_legumes", r"fruits? et legumes|primeur"),
    ("sucre", r"epicerie sucree|biscuit|confiserie|petit dejeuner"),
    ("epicerie", r"epicerie"),
]
NON_ALIMENTAIRE = {"animaux", "maison", "hygiene_maison", "autre"}


def singulier(txt):
    """norm() + mots au singulier (courgettes -> courgette, poireaux -> poireau)."""
    return " ".join(w[:-1] if len(w) > 3 and w[-1] in "sx" else w for w in norm(txt).split())


def categorie(nom, theme=""):
    t = norm(theme)
    if t:
        for cat, motif in THEMES:
            if re.search(motif, t):
                return cat
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
    if c.startswith("tel") or c.startswith("avantage ticket") or c == "ticket e leclerc":
        return "Ticket E.Leclerc"
    if c.startswith("brii"):
        return "Remise immédiate"
    if "offert" in c and re.search(r"\d", code):
        return propre(code)
    if "offert" in c:
        return "Produits offerts"
    if "xeme" in c or "eme produit" in c or "2e produit" in c:
        return "2e produit remisé"
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
        images = p.get("images") if isinstance(p.get("images"), list) else []
        image = next((i.get("url") for i in images if isinstance(i, dict) and i.get("url")), "") or ""
        produits.append({"nom": nom, "prix": fmt_prix(prix),
                         "prix_avant": fmt_prix(avant) if avant and avant > prix else "",
                         "remise": libelle_remise(p.get("discountLabel") or ""),
                         "theme": propre(p.get("thematic") or ""), "image": image,
                         "description": texte_html(p.get("description") or ""), "brut": p})
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


PROMO_VERSION = 2  # à augmenter quand la lecture des promos change : toutes les promos sont alors relues
OFFRES_INCONNUES = []  # exemples bruts d'offres sans montant, enregistrés dans debug/ pour améliorer la lecture


def lire_promos(catalogues):
    promos = {}
    for num, cat in catalogues.items():
        try:
            produits = produits_catalogue(cat)
        except Exception as e:
            avertissements.append(f"Catalogue {num} illisible : {e}")
            continue
        for p in produits:
            p["categorie"] = categorie(p["nom"], p.get("theme", ""))
        # un catalogue presque sans alimentaire (jouets, bricolage...) est ignoré
        alim = [p for p in produits if p["categorie"] not in NON_ALIMENTAIRE]
        if produits and len(alim) < 0.3 * len(produits):
            print(f"  catalogue {num} ignoré (non alimentaire)")
            continue
        for p in produits:
            cle = norm(p["nom"]) + "|" + p["prix"]
            if cle in promos:
                promos[cle]["magasins"] = sorted(set(promos[cle]["magasins"]) | set(cat["magasins"]))
                continue
            court, explication, pct = detail_remise(remise_calculee(p), p.get("description", ""), p["prix"],
                                                    p["prix_avant"], p.get("brut"))
            if court in ("Ticket E.Leclerc", "2e produit remisé") and len(OFFRES_INCONNUES) < 12:
                OFFRES_INCONNUES.append(p.get("brut", {}))
            promos[cle] = {
                "id": "p" + court_id(cle), "nom": p["nom"], "remise": court, "remise_detail": explication,
                "remise_pct": pct, "prix": p["prix"], "v": PROMO_VERSION,
                "prix_avant": p["prix_avant"], "categorie": p["categorie"], "magasins": list(cat["magasins"]),
                "du": cat["du"], "au": cat["au"], "catalogue": cat["titre"] or num,
                "catalogue_url": cat.get("url", ""), "image": p.get("image", ""),
                "description": p.get("description", ""), "rayon_leclerc": p.get("theme", ""),
            }
        print(f"  catalogue {num} : {len(produits)} produits")
    ordre = ["viande_poisson", "fruits_legumes", "cremerie", "traiteur", "epicerie", "sucre", "surgeles", "boissons",
             "hygiene_maison", "animaux", "maison", "autre"]
    return sorted(promos.values(), key=lambda p: (ordre.index(p["categorie"]), p["nom"]))


# ------------------------------------------------------------------ Lidl (catalogues lidl.fr)

def lidl_slugs_calcules():
    """Noms des catalogues « promos de la semaine » de Lidl (du jeudi au mercredi), si la page des catalogues
    ne répond pas : semaine en cours et semaine suivante."""
    jeudi = AUJOURDHUI - dt.timedelta(days=(AUJOURDHUI.weekday() - 3) % 7)
    slugs = []
    for debut in (jeudi, jeudi + dt.timedelta(days=7)):
        fin = debut + dt.timedelta(days=6)
        slugs.append(f"du-{debut:%d-%m}-au-{fin:%d-%m}-les-promos-de-la-semaine")
    return slugs


def lidl_catalogues():
    """Catalogues Lidl de la semaine (et celui qui commence dans les 2 jours) : liste lue sur lidl.fr, détails
    (dates, PDF) donnés par le service des catalogues en ligne de Lidl."""
    slugs = []
    try:
        soup, html = lire(LIDL_CATALOGUES_URL, "lidl_catalogues")
        for m in RE_LIDL_LEAFLET.finditer(html):
            if m.group(1) not in slugs and "exclus-web" not in m.group(1):
                slugs.append(m.group(1))
    except Exception as e:
        print(f"  page des catalogues Lidl illisible ({e}) : noms calculés")
    if not slugs:
        slugs = lidl_slugs_calcules()
    limite = (AUJOURDHUI + dt.timedelta(days=LIDL_JOURS_AVANCE)).isoformat()
    cats = []
    for slug in slugs[:6]:
        try:
            r = SESSION.get(LIDL_LEAFLET_API, params={"flyer_identifier": slug, "region_id": 0, "region_code": 0},
                            timeout=40)
            r.raise_for_status()
            f = (r.json() or {}).get("flyer") or {}
        except Exception as e:
            print(f"  catalogue Lidl {slug} illisible : {e}")
            continue
        du = (f.get("offerStartDate") or f.get("startDate") or "")[:10]
        au = (f.get("offerEndDate") or f.get("endDate") or "")[:10]
        pdf = f.get("hiResPdfUrl") or f.get("pdfUrl") or ""
        if not pdf or (au and au < AUJOURDHUI.isoformat()) or (du and du > limite):
            continue
        cats.append({"numero": slug, "url": f"https://www.lidl.fr/l/catalogue-de-la-semaine/{slug}/ar/0",
                     "titre": propre(f.get("title") or f.get("name") or "Catalogue Lidl"), "du": du, "au": au,
                     "pdf": f.get("pdfUrl") or pdf,
                     "liens": [l for pg in f.get("pages") or [] for l in (pg.get("links") or [])]})
    return cats


def lire_pdf(url):
    """Texte de chaque page d'un PDF (pypdf est installé à la volée s'il manque)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pypdf"], check=True)
        from pypdf import PdfReader
    import io
    r = SESSION.get(url, timeout=120)
    r.raise_for_status()
    lecteur = PdfReader(io.BytesIO(r.content))
    return [(page.extract_text() or "") for page in lecteur.pages]


# Lecture du texte des catalogues Lidl : chaque produit finit par son numéro d'article « no12345 » suivi des prix
# et de la remise ; le nom (marque en majuscules + nom) est juste avant, puis la description (« Le kilo », « 500 g »...).

RE_LIDL_ANCRE = re.compile(r"^n[o°]\s?\d{2,}(?:/\d+)?\s*(.*)$")
RE_LIDL_PRIX = re.compile(r"^(\d{1,4})\.\s?(\d{2})\s*\**$")
RE_LIDL_PCT = re.compile(r"^-\s?(\d{1,2})\s?%")
RE_LIDL_QUEUE = re.compile(r"^(le 2e produit|sur le 2e|offre flash|seulement|dont .*[ée]co|.*origine$|\(\d\)|\*+)", re.I)
RE_LIDL_BRUIT = re.compile(r"^(rayon|frais|surgel[ée]|toujours|plus de|promos|avec$|s\d{2}/\d{4}|du jeudi|du lundi|du mercredi|du dimanche|"
                      r"jeudi \d|lundi \d|mercredi \d|dimanche \d|suggestions? de|photos? non|plus d'informations|pour votre sant|"
                      r"les articles de cette|lidl\.fr|commandez|uniquement en ligne|moins cher|opportunit|dernière minute|"
                      r"jusqu'à|à partir de|exclusivit|nouveau|nouveauté|offre flash|seulement|-\s?\d+\s?%|sur le 2e|le 2e produit|"
                      r"\d{1,4}\.\s?\d{2}\s*$|.*origine$|\*|.*transform[ée]|le mois|plus ?plus|toujours|.*[.!?,]$|-|.*€)", re.I)
RE_LIDL_DESCR = re.compile(r"^(le produit|les \d|la barquette|le kilo|la pi[èe]ce|le sachet|le filet|l['’]unit[ée]|le lot|le paquet|"
                      r"le pack|la bo[iî]te|la bouteille|le pot|la botte|le bouquet|le flow|cat[ée]gorie|calibres?|vari[ée]t[ée]s?|"
                      r"dimensions|nature|traitement|\d+[\s,.]*\d*\s?(x|g|kg|ml|cl|l|pi[èe]ces|lavages|w)\b|\(|avec lidl|au lieu|"
                      r".* : |.*: .*|le carton|la caisse|la bourriche|le plateau|la tranche|les 2|taille|pointure|coloris|capacit|puissance|mod[èe]les?)", re.I)


RE_LIDL_MARQUE = re.compile(r"(?:[A-ZÀ-Ý][A-ZÀ-Ý0-9'’&.\-]+(?:\s+|$)){1,4}(?=[A-ZÀ-Ý]?[a-zà-ÿ]|$)")
RE_LIDL_COUPE = re.compile(r"\s+(Au choix|Prix normal|Env\.|Ex\.|Du \d|Avec batterie|Usage|Dimensions|Dont|L ?['’]unit).*$")


RE_LIDL_DEBUT = re.compile(r"^(?:L ?['’]unit\S*.*?OFFERTS?\s+|(?:offerts?|offertes?|xxl|exclu|nouveau)\s+)+", re.I)
LIDL_GARDER_MAJ = {"BBQ", "ASC", "USB", "LED", "UNO", "XXL", "BIO", "AOP", "IGP", "AOC"}


def lidl_est_marque(l):
    lettres = re.sub(r"[^A-Za-zÀ-ÿ]", "", l)
    return len(lettres) >= 2 and lettres == lettres.upper() and len(l) <= 30




def produits_lidl_texte(pages):
    produits = []
    for texte in pages:
        lignes = [re.sub(r"\s+", " ", l).strip() for l in texte.split("\n")]
        lignes = [l for l in lignes if l]
        debut = 0
        i = 0
        while i < len(lignes):
            m = RE_LIDL_ANCRE.match(lignes[i])
            if not m:
                i += 1
                continue
            bloc = lignes[debut:i]
            # queue : prix, remises, origines
            prix, pct, second = [], None, False
            reste = m.group(1).strip()
            j = i + 1
            queue = ([reste] if reste else []) + lignes[j:j + 8]
            n = 0
            for l in queue:
                mp = RE_LIDL_PRIX.match(l)
                mq = RE_LIDL_PCT.match(l)
                if mp:
                    prix.append(float(f"{mp.group(1)}.{mp.group(2)}"))
                elif mq:
                    pct = pct or int(mq.group(1))
                elif re.match(r"^(le 2e produit|sur le 2e)", l, re.I):
                    second = True
                elif RE_LIDL_QUEUE.match(l):
                    pass
                else:
                    break
                n += 1
            consommees = n - (1 if reste else 0)
            debut = i + 1 + max(consommees, 0)
            i = debut
            if not prix:
                continue
            # nom : marque (majuscules) + lignes jusqu'à la description
            utiles = [l for l in bloc if not RE_LIDL_BRUIT.match(l)]
            marque, nom, descr = [], [], []
            etat = "marque"
            for l in utiles:
                if etat in ("marque", "nom") and not RE_LIDL_DESCR.match(l):
                    if etat == "marque" and lidl_est_marque(l) and not nom:
                        marque.append(l)
                        continue
                    etat = "nom"
                    nom.append(l)
                else:
                    etat = "descr"
                    descr.append(l)
            if not nom and not marque:
                continue
            nom_txt = " ".join(nom)
            # un bloc peut traîner du texte du produit précédent : on garde la fin
            if len(nom_txt) > 70 and len(nom) > 3:
                nom_txt = " ".join(nom[-3:])
            brut = re.sub(r"\s*\(\d\)\s*", " ", (" ".join(marque) + " " + nom_txt)).strip()
            # du texte publicitaire avant la marque (« dans vos MARIBEL Confiture ») : on part de la marque
            mm = RE_LIDL_MARQUE.search(brut)
            if mm and mm.start() > 0 and RE_LIDL_MARQUE.match(brut) is None:
                brut = brut[mm.start():]
            brut = RE_LIDL_COUPE.sub("", brut)
            mm = RE_LIDL_MARQUE.match(brut)
            nom_complet = (mm.group(0).strip().title() + " " + brut[mm.end():].strip()).strip() if mm else brut
            nom_complet = RE_LIDL_DEBUT.sub("", nom_complet)
            nom_complet = re.sub(r"\b[A-ZÀ-Ý][A-ZÀ-Ý'’\-]{2,}\b",
                                 lambda w: w.group(0) if w.group(0) in LIDL_GARDER_MAJ else w.group(0).title(), nom_complet)
            nom_complet = re.sub(r"\s+", " ", nom_complet)
            nom_complet = re.sub(r"^[\d.,\s]+(?=[A-Za-zÀ-ÿ])", "", nom_complet).strip().rstrip("*").strip()
            texte_bloc = " ".join(bloc)
            lidl_plus = "avec lidl plus" in texte_bloc.lower()
            haut, bas = max(prix), min(prix)
            if second:
                # « 2e produit à -68 % » : le prix affiché en gros est celui du 2e ; on montre le prix d'un produit
                mlot = re.search(r"Les 2 produits\s*:?\s*(\d+,\d{2})\s*€", texte_bloc)
                remise = f"-{pct} % sur le 2e" if pct else "2e produit remisé"
                detail = f"Le 2e produit à {fmt_prix(bas)} : les 2 pour {mlot.group(1)} €." if mlot else f"Le 2e produit à {fmt_prix(bas)}."
                produits.append({"nom": nom_complet, "prix": fmt_prix(haut), "prix_avant": "", "remise": remise,
                                 "remise_detail": detail, "remise_pct": pct, "description": " · ".join(descr)[:300]})
            else:
                avant = haut if haut > bas * 1.03 else 0
                remise = (f"-{pct} %" if pct else "") + (" avec Lidl Plus" if lidl_plus and pct else "")
                detail = "Prix avec l'appli Lidl Plus (carte de fidélité)." if lidl_plus else ""
                produits.append({"nom": nom_complet, "prix": fmt_prix(bas), "prix_avant": fmt_prix(avant) if avant else "",
                                 "remise": remise.strip(), "remise_detail": detail, "remise_pct": pct,
                                 "description": " · ".join(descr)[:300]})
    return produits


def lidl_produits(cat):
    """Produits d'un catalogue Lidl : texte du PDF du catalogue + produits liés (pages produit lidl.fr)."""
    pages = lire_pdf(cat["pdf"])
    nom_debug = "lidl_pdf_" + cat["numero"][:40]
    if debug_actif or not (DEBUG / f"{nom_debug}.txt").exists():
        DEBUG.mkdir(exist_ok=True)
        texte = "\n\n=== PAGE ===\n".join(pages)
        (DEBUG / f"{nom_debug}.txt").write_text(texte[:300000], encoding="utf-8")
    return produits_lidl_texte(pages)


RE_LIDL_PAS_PRODUIT = re.compile(r"^(lidl|catalogue|promo|offre|magasin|horaires|voir|page|prospectus|123catalogue)\b", re.I)


LIDL_MARQUES_MAISON = re.compile(r"^(parkside|silvercrest|livarno|crivit|esmara|livergy|lupilu|pepperts|tronic|melinera|"
                                 r"ernesto|auriol|sensiplast|ultimate speed|powerfix|florabest|playtive|zoofari|anker|remington|"
                                 r"mattel|lego|philips|tefal|moulinex)\b", re.I)
LIDL_MARQUES_HYGIENE = re.compile(r"^(w5|cien|purio|formil|doussy|sanytol|mr\.? propre|ushua|le petit marseillais|vania|always|"
                                  r"oral[ -]?b|colgate|nivea|dove|airwick|tempo|lotus)\b", re.I)


def categorie_lidl(nom, description=""):
    """Rayon d'un produit Lidl : marques maison/non-alimentaires de Lidl, puis mots du nom, puis indices de la description."""
    if LIDL_MARQUES_MAISON.match(nom):
        return "maison"
    if LIDL_MARQUES_HYGIENE.match(nom):
        return "hygiene_maison"
    cat = categorie(nom)
    if cat != "autre":
        return cat
    d = norm(description)
    if re.search(r"\bcategorie\b|\bcalibre|\bvariete", d):
        return "fruits_legumes"
    if re.search(r"\bpot\b|en coupe|bulbe|plante|fleur|orchidee|rosier|azalee", norm(nom)):
        return "maison"
    if re.search(r"\b\d+\s?(g|kg|ml|cl|l)\b|le kilo|1 kg|1 l\b", d):
        return "epicerie"
    return "autre"


def collecter_lidl(ancien):
    """Promos du Lidl du Houlme. Ne relit les catalogues que s'ils ont changé depuis la fois précédente."""
    anciennes = [p for p in ancien.get("promos", []) if LIDL["id"] in p.get("magasins", [])
                 and (not p.get("au") or p["au"] >= AUJOURDHUI.isoformat())]
    cats = lidl_catalogues()
    nums = sorted(c["numero"] for c in cats)
    if not cats:
        avertissements.append("Aucun catalogue Lidl trouvé : promos Lidl de la fois précédente conservées")
        return anciennes, ancien.get("lidl_catalogues_vus", [])
    meme_version = all(c.get("v") == LIDL_VERSION for c in ancien.get("lidl_catalogues_vus", []))
    if (nums == sorted(c.get("numero", "") for c in ancien.get("lidl_catalogues_vus", [])) and anciennes
            and meme_version and not debug_actif):
        print("  catalogues Lidl inchangés")
        return anciennes, ancien.get("lidl_catalogues_vus", [])
    promos = {}
    for cat in cats:
        try:
            produits = lidl_produits(cat)
        except Exception as e:
            avertissements.append(f"Catalogue Lidl {cat['numero']} illisible : {e}")
            continue
        print(f"  catalogue Lidl {cat['numero']} ({cat['du']} → {cat['au']}) : {len(produits)} produits")
        for p in produits:
            if RE_LIDL_PAS_PRODUIT.match(p["nom"]):
                continue
            cle = norm(p["nom"]) + "|" + p["prix"]
            if cle in promos:
                continue
            promos[cle] = {
                "id": "l" + court_id("lidl", cle), "nom": p["nom"], "remise": p["remise"],
                "remise_detail": p.get("remise_detail", ""), "remise_pct": p.get("remise_pct"), "prix": p["prix"],
                "v": PROMO_VERSION, "prix_avant": p["prix_avant"], "categorie": categorie_lidl(p["nom"], p.get("description", "")),
                "magasins": [LIDL["id"]], "du": cat["du"], "au": cat["au"],
                "catalogue": (cat["titre"] or "Catalogue Lidl") + (f" du {cat['du'][8:10]}/{cat['du'][5:7]}" if cat["du"] else ""),
                "catalogue_url": cat["url"], "image": "", "description": p.get("description", ""),
                "rayon_leclerc": "", "enseigne": "Lidl",
            }
    if not promos:
        avertissements.append("Aucun produit lu dans les catalogues Lidl : promos Lidl de la fois précédente conservées")
        return anciennes, ancien.get("lidl_catalogues_vus", [])
    vus = [{"numero": c["numero"], "titre": c["titre"], "url": c["url"], "du": c["du"], "au": c["au"],
            "magasins": [LIDL["id"]], "v": LIDL_VERSION} for c in cats]
    return list(promos.values()), sorted(vus, key=lambda c: c["numero"])


# ------------------------------------------------------------------ Action

RE_ACTION_PRODUIT = re.compile(r"/fr-fr/p/(\d+)/")
RE_ACTION_PRIX = re.compile(r"(\d{1,4})\s*[,.]\s*(\d{2})\s*€?")


def action_produits_json(html):
    """Produits trouvés dans les données JSON de la page (Next.js, JSON-LD), si le site en fournit."""
    blocs = re.findall(r'<script[^>]*(?:__NEXT_DATA__|application/ld\+json|application/json)[^>]*>(.*?)</script>', html, re.S)
    trouves = {}

    def visiter(o):
        if isinstance(o, dict):
            nom = o.get("name") or o.get("title")
            ident = str(o.get("id") or o.get("sku") or o.get("productId") or "")
            prix = None
            for k in ("price", "sellingPrice", "currentPrice", "salesPrice"):
                v = o.get(k)
                if isinstance(v, dict):
                    v = v.get("value") or v.get("amount") or v.get("price")
                if v not in (None, ""):
                    prix = nombre(v)
                    break
            if prix is None and isinstance(o.get("offers"), dict):
                prix = nombre(o["offers"].get("price"))
            url = o.get("url") or o.get("href") or o.get("slug") or ""
            m = RE_ACTION_PRODUIT.search(str(url))
            if isinstance(nom, str) and prix and (m or ident.isdigit()):
                num = m.group(1) if m else ident
                img = o.get("image")
                if isinstance(img, list):
                    img = img[0] if img else ""
                if isinstance(img, dict):
                    img = img.get("url") or img.get("src") or ""
                trouves.setdefault(num, {"num": num, "nom": propre(nom), "prix": prix,
                                         "url": urljoin("https://www.action.com", str(url)) if url else "",
                                         "image": img if isinstance(img, str) else ""})
            for v in o.values():
                visiter(v)
        elif isinstance(o, list):
            for v in o:
                visiter(v)

    for b in blocs:
        try:
            visiter(json.loads(b))
        except Exception:
            pass
    return list(trouves.values())


def action_produits_cartes(soup):
    """Cartes produit d'action.com (data-testid « product-card ») : titre, détail, prix en deux morceaux."""
    produits = []
    for c in soup.find_all(attrs={"data-testid": "product-card"}):
        def champ(nom):
            el = c.find(attrs={"data-testid": nom})
            return propre(el.get_text(" ")) if el else ""
        lien = c.find("a", href=RE_ACTION_PRODUIT)
        titre, entier, centimes = champ("product-card-title"), champ("product-card-price-whole"), champ("product-card-price-fractional")
        if not (lien and titre and entier.isdigit()):
            continue
        centimes = centimes if centimes.isdigit() else "00"
        img = c.find(attrs={"data-testid": "product-card-image"}) or c.find("img")
        produits.append({"num": RE_ACTION_PRODUIT.search(lien["href"]).group(1), "nom": titre,
                         "prix": float(f"{entier}.{centimes[:2].ljust(2, '0')}"),
                         "detail": champ("product-card-description"),
                         "prix_unite": champ("product-card-price-description"),
                         "url": urljoin("https://www.action.com", lien["href"]),
                         "image": (img.get("src") or "") if img else ""})
    return produits


def action_produits_html(soup):
    """Produits lus dans les cartes de la page : un lien /fr-fr/p/<numéro>/ par produit, le prix dans la carte."""
    cartes = {}
    for a in soup.find_all("a", href=True):
        m = RE_ACTION_PRODUIT.search(a["href"])
        if m:
            cartes.setdefault(m.group(1), []).append(a)
    produits = []
    for num, liens in cartes.items():
        # la carte = le plus petit bloc parent qui contient un prix
        bloc, texte = None, ""
        for el in [liens[0]] + list(liens[0].parents)[:6]:
            t = propre(el.get_text(" "))
            if RE_ACTION_PRIX.search(t) or re.search(r"\d+\s*,\s*\d{2}|\d+\s+\d{2}\s*€", t):
                bloc, texte = el, t
                break
        if bloc is None:
            continue
        nom = ""
        for a in liens:
            nom = propre(a.get("title") or a.get("aria-label") or "")
            if nom:
                break
        if not nom:
            titre = bloc.find(["h2", "h3", "h4"]) or bloc.find(attrs={"data-testid": re.compile("title|name", re.I)})
            nom = propre(titre.get_text(" ")) if titre else ""
        if not nom:
            img = bloc.find("img", alt=True)
            nom = propre(img["alt"]) if img else ""
        if not nom:
            continue
        # prix principal : le premier prix qui n'est pas un prix au kilo / au mètre
        sans_unitaires = re.sub(r"\d+\s*[,.]\s*\d{2}\s*€\s*/\s*(?:m\d?|kg|l|100\s*\w*)\b", " ", texte)
        m = RE_ACTION_PRIX.search(sans_unitaires.replace(nom, " "))
        if not m:
            # prix écrit en deux morceaux (« 4 » « 69 » ou « 4 69 € »)
            m2 = re.search(r"(?<![\d,.])(\d{1,4})\s+(\d{2})(?:\s*€|(?=\s|$))", sans_unitaires.replace(nom, " "))
            if not m2:
                continue
            prix = float(f"{m2.group(1)}.{m2.group(2)}")
        else:
            prix = float(f"{m.group(1)}.{m.group(2)}")
        img = bloc.find("img")
        image = ""
        if img:
            image = img.get("src") or img.get("data-src") or ""
            if not image and img.get("srcset"):
                image = img["srcset"].split(",")[-1].strip().split(" ")[0]
        produits.append({"num": num, "nom": nom, "prix": prix,
                         "url": urljoin("https://www.action.com", liens[0]["href"]), "image": image})
    return produits


RE_ACTION_HYGIENE = re.compile(r"blush|mascara|vernis|rouge a levres|maquillage|fond de teint|creme|shampo|douche|savon|"
                               r"deodorant|dentifrice|brosse a dents|pansement|lingette|coton tige|disques? de coton|coton demaquillant|parfum|eau de toilette|"
                               r"lessive|nettoyant|eponge|papier toilette|mouchoir|couche|serum|masque|gel|lotion|rasoir|"
                               r"sac poubelle|liquide vaisselle|tablettes? lave", re.I)
RE_ACTION_CONTENANT = re.compile(r"pot|boite|bocal|gourde|bouteille|flacon|bidon|seau|vase|bougie|diffuseur|peinture|colle", re.I)


def categorie_action(nom, detail=""):
    """Rayon d'un produit Action : surtout du non-alimentaire, l'alimentaire se reconnaît à son poids / volume."""
    n = norm(nom)
    cat = categorie(nom)
    if cat == "animaux":
        return cat
    if RE_ACTION_HYGIENE.search(n):
        return "hygiene_maison"
    vendu_au_poids = re.search(r"\b\d+(?:[,.]\d+)?\s?(?:g|kg|cl|ml|l)\b", detail or "")
    if vendu_au_poids and not RE_ACTION_CONTENANT.search(n):
        return cat if cat not in NON_ALIMENTAIRE else "epicerie"
    return "maison"


def collecter_action():
    """Affaires de la semaine Action (toutes les pages). Renvoie (produits, du, au)."""
    produits, du, au = {}, "", ""
    for page in range(1, ACTION_PAGES_MAX + 1):
        url = ACTION_URL if page == 1 else f"{ACTION_URL}?page={page}"
        soup, html = lire(url, f"action_page{page}")
        if page == 1:
            dates = dates_dans(propre(soup.get_text(" "))[:20000])
            semaine = [d for d in dates if abs((d - AUJOURDHUI).days) <= 10]
            if len(semaine) >= 2:
                du, au = min(semaine[:2]).isoformat(), max(semaine[:2]).isoformat()
        trouves = action_produits_cartes(soup) or action_produits_json(html) or action_produits_html(soup)
        nouveaux = [p for p in trouves if p["num"] not in produits]
        for p in nouveaux:
            produits[p["num"]] = p
        print(f"  Action page {page} : {len(trouves)} produits ({len(nouveaux)} nouveaux)")
        if not nouveaux or f"page={page + 1}" not in html:
            break
    if len(produits) < 5 or not (DEBUG / "action_page1.html").exists():
        # copie de la page (une seule fois, ou quand la lecture échoue) pour pouvoir ajuster la lecture
        sauver_debug("action_page1", lire(ACTION_URL, "action_page1")[1])
    if not au:
        # semaine Action : du mercredi au mardi
        debut = AUJOURDHUI - dt.timedelta(days=(AUJOURDHUI.weekday() - 2) % 7)
        du, au = debut.isoformat(), (debut + dt.timedelta(days=6)).isoformat()
    promos = []
    for p in produits.values():
        cat = categorie_action(p["nom"], p.get("detail", ""))
        promos.append({
            "id": "a" + court_id("action", p["num"], fmt_prix(p["prix"])), "nom": p["nom"],
            "remise": "Affaire de la semaine", "remise_detail": "Prix bas Action, valable dans tous les magasins Action.",
            "remise_pct": None, "prix": fmt_prix(p["prix"]), "v": PROMO_VERSION, "prix_avant": "",
            "categorie": cat, "magasins": [ACTION["id"]], "du": du, "au": au,
            "catalogue": "Les affaires du moment", "catalogue_url": p["url"] or ACTION_URL,
            "image": p["image"], "description": " | ".join(x for x in [p.get("detail", ""), p.get("prix_unite", "")] if x),
            "detail": p.get("detail", ""), "rayon_leclerc": "", "enseigne": "Action",
        })
    return promos


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
    if p.get("remise_pct"):
        return 1 - p["remise_pct"] / 100
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


# ------------------------------------------------------------------ photos des plats (Wikipédia / Wikimedia Commons)

PHOTOS_CACHE = RACINE / "data" / "photos_recettes.json"
WIKI_UA = {"User-Agent": "PanierMalin/1.0 (https://github.com/davidbdesk76-hash/panier-malin; appli perso)"}


def photo_wiki(requete):
    """Photo (vignette 800 px) de l'article Wikipédia du plat : titre exact d'abord, sinon recherche (fr puis en)."""
    base = {"action": "query", "format": "json", "prop": "pageimages", "piprop": "thumbnail",
            "pithumbsize": 800, "redirects": 1}
    if requete.startswith("en:"):  # article anglais demandé explicitement (« en:Pork chop »)
        requete = requete[3:]
        essais = [("en", {**base, "titles": requete}),
                  ("en", {**base, "generator": "search", "gsrsearch": requete, "gsrlimit": 3})]
    else:
        essais = [("fr", {**base, "titles": requete}),
                  ("fr", {**base, "generator": "search", "gsrsearch": requete, "gsrlimit": 3}),
                  ("en", {**base, "generator": "search", "gsrsearch": requete, "gsrlimit": 3})]
    for langue, params in essais:
        r = SESSION.get(f"https://{langue}.wikipedia.org/w/api.php", params=params, headers=WIKI_UA, timeout=20)
        r.raise_for_status()
        pages = sorted((r.json().get("query") or {}).get("pages", {}).values(), key=lambda x: x.get("index", 0))
        for pg in pages:
            src = (pg.get("thumbnail") or {}).get("source", "")
            if src and not src.lower().endswith((".svg.png", ".gif")):
                return src
    return ""


def photos_recettes(base):
    """Photo de chaque plat, gardée dans data/photos_recettes.json (on ne cherche que les nouvelles)."""
    cache = json.loads(PHOTOS_CACHE.read_text(encoding="utf-8")) if PHOTOS_CACHE.exists() else {}
    change, erreurs = False, 0
    for r in base:
        q = r.get("wiki", r["titre"])
        if not q or q in cache or r.get("photo"):
            continue
        try:
            cache[q] = photo_wiki(q)
            change = True
        except Exception as e:
            erreurs += 1
            if erreurs >= 3:
                avertissements.append(f"Photos des plats indisponibles pour l'instant ({e})")
                break
    if change:
        PHOTOS_CACHE.parent.mkdir(exist_ok=True)
        PHOTOS_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return cache


def tete_produit(nom):
    """Début du nom du produit, sans la marque ni le nombre : « 8 saucisses de toulouse — X » -> « saucisse de toulouse »."""
    t = singulier(nom.split(" — ")[0])
    return re.sub(r"^(lot de |x ?)?\d+( x)? ", "", t)


NOTE_PRIX = ("Prix de revient approximatif : les ingrédients hors promo sont estimés (moyennes Open Prices "
             "ou estimations), sauf ceux relevés sur vos tickets de caisse.")


def choisir_recettes(promos, n=10):
    base = json.loads((RACINE / "scripts" / "recettes_base.json").read_text(encoding="utf-8"))
    table = table_des_prix()
    photos = photos_recettes(base)
    base = [r for r in base if r["temps_min"] <= RECETTE_TEMPS_MAX and len(r["ingredients"]) <= RECETTE_INGREDIENTS_MAX
            and len(r["etapes"]) <= RECETTE_ETAPES_MAX]
    par_id = {p["id"]: p for p in promos}
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
                      # photos des produits en promo de la recette (pour illustrer la carte dans l'appli)
                      "images": [par_id[i["promo_id"]]["image"] for i in ings
                                 if i["promo_id"] and par_id.get(i["promo_id"], {}).get("image")][:3],
                      "photo": r.get("photo") or photos.get(r.get("wiki", r["titre"]), ""),
                      "photo_credit": "Photo : Wikimédia Commons"
                      if not r.get("photo") and photos.get(r.get("wiki", r["titre"])) else "",
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


# boutons et compteurs de PanneauPocket (« 1 sur 14 », « Sur Twitter », « Copier le lien »…)
RE_PARTAGE = re.compile(r"(\d+ sur \d+|sur (twitter|facebook|x|whatsapp|linkedin)|par (e ?mail|sms)|copier le lien|"
                        r"partager( sur .*)?|imprimer|fermer|telecharger|agrandir)")


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
                  and not RE_PARTAGE.fullmatch(norm(l))
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


DETAILS_CACHE = {}  # url -> {"texte", "lieu", "image"}, rempli avec les données précédentes dans main()


def detail_page(url):
    """Texte complet, lieu et affiche d'une page d'actualité ou d'événement de le-houlme.fr."""
    if url in DETAILS_CACHE:
        return DETAILS_CACHE[url]
    soup, html = lire(url, "detail")
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    image = soup.find("meta", attrs={"property": "og:image"})
    for t in soup(["script", "style", "nav", "footer", "header", "form", "aside"]):
        t.decompose()
    zone = soup.find("main") or soup.find("article") or soup.body or soup
    paras = []
    for el in zone.find_all(["p", "li"]):
        x = propre(el.get_text(" "))
        if len(x) >= 35 and x not in paras and not RE_PARTAGE.fullmatch(norm(x)):
            paras.append(x)
    texte = "\n".join(paras) or propre(meta.get("content", "")) if meta else "\n".join(paras)
    plat = propre(zone.get_text(" "))
    lieu = re.search(r"\bLieu\s*:?\s*(.{3,60}?)(?:\s+(?:Dates?|Horaires?|Heure|Adresse|Contact|Tarif)\b|$)", plat)
    d = {"texte": texte[:2000], "lieu": propre(lieu.group(1)) if lieu else "",
         "image": urljoin(url, image["content"]) if image and image.get("content") else ""}
    DETAILS_CACHE[url] = d
    return d


def avec_detail(item):
    """Complète un élément (actu ou événement de le-houlme.fr) avec le texte de sa page."""
    try:
        d = detail_page(item["url"])
    except Exception:
        return item
    if d["texte"]:
        item["texte"] = d["texte"]
    if d["image"]:
        item["image"] = d["image"]
    if d["lieu"] and not item.get("lieu"):
        item["lieu"] = d["lieu"]
    return item


# ------------------------------------------------------------------ transports (réseau Astuce)

# Lignes qui desservent Le Houlme (site de la mairie : 29, F4 ; 360 vers le collège Jean Zay)
LIGNES_HOULME = ["F4", "29", "360"]
ASTUCE_ALERTES = [
    "https://api.mrn.cityway.fr/dataflow/info-transport/download?provider=ASTUCE&dataFormat=gtfs-rt",
    "https://hexatransit.fr/datasets/services_rt/astuce/service_alerts.pb",  # copie de secours
]
ASTUCE_GTFS = "https://api.mrn.cityway.fr/dataflow/offre-tc/download?provider=ASTUCE&dataFormat=gtfs&dataProfil=ASTUCE"
LIGNES_CACHE = RACINE / "data" / "astuce_lignes.json"
EFFETS = {1: "Service interrompu", 2: "Service réduit", 3: "Retards importants", 4: "Déviation",
          5: "Service renforcé", 6: "Service modifié", 9: "Arrêt déplacé", 11: "Accessibilité"}
CAUSES = {3: "Problème technique", 4: "Grève", 5: "Manifestation", 6: "Accident", 8: "Météo",
          9: "Maintenance", 10: "Travaux", 11: "Intervention de police", 12: "Urgence médicale"}


def pb_lire(data):
    """Petit lecteur protobuf (sans dépendance) : {numéro de champ: [valeurs]} ; bytes pour les sous-messages."""
    champs, i, n = {}, 0, len(data)

    def varint():
        nonlocal i
        v, decal = 0, 0
        while True:
            b = data[i]
            i += 1
            v |= (b & 0x7F) << decal
            if b < 0x80:
                return v
            decal += 7

    while i < n:
        cle = varint()
        num, type_ = cle >> 3, cle & 7
        if type_ == 0:
            val = varint()
        elif type_ == 1:
            val = data[i:i + 8]; i += 8
        elif type_ == 2:
            longueur = varint()
            val = data[i:i + longueur]; i += longueur
        elif type_ == 5:
            val = data[i:i + 4]; i += 4
        else:
            raise ValueError("protobuf illisible")
        champs.setdefault(num, []).append(val)
    return champs


def pb_texte(ts):
    """TranslatedString -> texte (français de préférence)."""
    textes = []
    for tr in pb_lire(ts).get(1, []):
        t = pb_lire(tr)
        txt = t.get(1, [b""])[0].decode("utf-8", "replace")
        langue = t.get(2, [b""])[0].decode("utf-8", "replace").lower()
        textes.append((0 if langue.startswith("fr") else 1 if not langue else 2, txt))
    return propre(sorted(textes)[0][1]) if textes else ""


def lignes_astuce():
    """route_id -> numéro de ligne (« F4 », « 29 »…), lu dans le GTFS et gardé une semaine."""
    cache = json.loads(LIGNES_CACHE.read_text(encoding="utf-8")) if LIGNES_CACHE.exists() else {}
    if cache.get("date", "") >= (AUJOURDHUI - dt.timedelta(days=7)).isoformat() and cache.get("routes"):
        return cache["routes"]
    import csv, io, zipfile
    r = SESSION.get(ASTUCE_GTFS, timeout=90)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        nom = next(x for x in z.namelist() if x.endswith("routes.txt"))
        texte = z.read(nom).decode("utf-8-sig")
    routes = {row["route_id"]: (row.get("route_short_name") or row.get("route_long_name") or "").strip()
              for row in csv.DictReader(io.StringIO(texte))}
    LIGNES_CACHE.parent.mkdir(exist_ok=True)
    LIGNES_CACHE.write_text(json.dumps({"date": AUJOURDHUI.isoformat(), "routes": routes}, ensure_ascii=False),
                            encoding="utf-8")
    return routes


RE_LIGNES_TEXTE = re.compile(r"\blignes?\s+((?:[A-Z]?\d{1,3}|T\d|F\d)(?:\s*(?:,|et|/|-)\s*(?:[A-Z]?\d{1,3}|T\d|F\d))*)", re.I)


def collecter_transport():
    try:
        routes = lignes_astuce()
    except Exception as e:
        routes = {}
        print(f"  lignes Astuce non lues : {e}")
    contenu, derniere_erreur = None, None
    for url in ASTUCE_ALERTES:
        try:
            r = SESSION.get(url, timeout=40)
            r.raise_for_status()
            contenu = r.content
            break
        except Exception as e:
            derniere_erreur = e
    if contenu is None:
        avertissements.append(f"Info trafic Astuce inaccessible : {derniere_erreur}")
        return None
    maintenant = int(MAINTENANT.timestamp())
    dans_7_jours = maintenant + 7 * 86400
    alertes, vues = [], set()
    for ent in pb_lire(contenu).get(2, []):
        e = pb_lire(ent)
        if not e.get(5):
            continue
        a = pb_lire(e[5][0])
        # période : en cours ou qui commence dans les 7 jours
        periodes = [pb_lire(x) for x in a.get(1, [])]
        debut = min((p.get(1, [0])[0] for p in periodes), default=0)
        fin = max((p.get(2, [0])[0] for p in periodes), default=0)
        if periodes and not any((p.get(1, [0])[0] or 0) <= dans_7_jours and (not p.get(2) or p[2][0] >= maintenant)
                                for p in periodes):
            continue
        titre = texte_html(pb_texte(a[10][0])) if a.get(10) else ""
        texte = texte_html(pb_texte(a[11][0]), 1500) if a.get(11) else ""
        lien = pb_texte(a[8][0]) if a.get(8) else ""
        lignes, reseau = set(), False
        for sel in a.get(5, []):
            s_ = pb_lire(sel)
            if s_.get(2):
                lignes.add(routes.get(s_[2][0].decode(), s_[2][0].decode()))
            elif s_.get(1) and not s_.get(5) and not s_.get(4):
                reseau = True
        for m in RE_LIGNES_TEXTE.finditer(titre + " " + texte):
            lignes |= {x.upper() for x in re.findall(r"[A-Z]?\d{1,3}|T\d|F\d", m.group(1), re.I)}
        concernees = sorted(l for l in lignes if l.upper() in LIGNES_HOULME)
        cause = CAUSES.get(a.get(6, [0])[0], "")
        greve = cause == "Grève" or bool(re.search(r"gr[eè]ve|mouvement social", norm(titre + " " + texte)))
        effet = a.get(7, [0])[0]
        tout = norm(titre + " " + texte)
        perturbation = greve or effet in (1, 2, 3, 4, 6, 9) or bool(re.search(
            r"perturb|interromp|devie|deviation|retard|non desservi|ne circule|supprime|travaux|incident|neige|verglas", tout))
        if not concernees:
            # alerte sans ligne précise : seulement une vraie perturbation de tout le réseau
            # (pas les actualités du réseau, ni une grève limitée à un autre secteur comme Elbeuf)
            generale = bool(re.search(r"ensemble du reseau|tout le reseau|toutes les lignes|houlme", tout))
            autre_secteur = bool(re.search(r"secteur|elbeuf|uniquement|rive gauche", tout))
            # (les « travaux » d'un théâtre ou d'un stade ne sont pas une perturbation des bus : mots plus stricts ici)
            forte = greve or effet in (1, 2, 3) or bool(re.search(
                r"perturb|interromp|ne circule|non desservi|aucun bus|aucune ligne|reseau (est )?(arrete|interrompu)", tout))
            if lignes or not ((reseau or greve) and forte and (generale or not autre_secteur)):
                continue
        elif not perturbation and effet in (0, 5, 7, 8):
            continue
        cle = norm(titre)[:60]
        if cle in vues:
            continue
        vues.add(cle)
        alertes.append({
            "titre": titre or "Perturbation",
            "texte": texte[:2000],
            "lignes": concernees or ["Tout le réseau"],
            "type": "Grève" if greve else EFFETS.get(a.get(7, [0])[0], cause or "Perturbation"),
            "debut": dt.datetime.fromtimestamp(debut, TZ).strftime("%Y-%m-%dT%H:%M") if debut else "",
            "fin": dt.datetime.fromtimestamp(fin, TZ).strftime("%Y-%m-%dT%H:%M") if fin else "",
            "url": lien,
        })
    alertes.sort(key=lambda x: (x["debut"] > MAINTENANT.strftime("%Y-%m-%dT%H:%M"), x["debut"]))
    return {"reseau": "Astuce", "lignes": LIGNES_HOULME, "alertes": alertes[:20],
            "maj": MAINTENANT.strftime("%Y-%m-%dT%H:%M"),
            "source": "Réseau Astuce - Métropole Rouen Normandie (données ouvertes)"}


def collecter_ville():
    limite_actus = (AUJOURDHUI - dt.timedelta(days=45)).isoformat()
    limite_alertes = (AUJOURDHUI - dt.timedelta(days=90)).isoformat()
    alertes, actus, agenda, ok = [], [], [], 0

    try:
        for it in panneaupocket():
            ok += 1
            if est_alerte(it["titre"]) and (not it["date"] or it["date"] >= limite_alertes):
                alertes.append({"titre": it["titre"], "texte": it["texte"][:2000], "source": "PanneauPocket", "url": it["url"]})
            elif not it["date"] or it["date"] >= limite_actus:
                actus.append({"titre": it["titre"], "date": it["date"], "resume": resume(it["texte"]),
                              "texte": it["texte"][:2000], "source": "PanneauPocket", "url": it["url"]})
    except Exception as e:
        avertissements.append(f"PanneauPocket inaccessible : {e}")

    try:
        for it in site_mairie("actus", r"/actualite/\d+"):
            ok += 1
            d = it["dates"][0].isoformat() if it["dates"] else ""
            if d and d < limite_actus:
                continue
            actus.append(avec_detail({"titre": it["titre"], "date": d, "resume": resume(it["texte"]),
                                      "source": "le-houlme.fr", "url": it["url"]}))
    except Exception as e:
        avertissements.append(f"Actualités le-houlme.fr inaccessibles : {e}")

    try:
        for it in site_mairie("agenda", r"/(agenda|evenement)[^\"']*\d+"):
            ok += 1
            futures = [d for d in it["dates"] if d >= AUJOURDHUI]
            if not futures:
                continue
            lieu = re.search(r"(?:Lieu|Adresse)\s*:?\s*([^|•]{3,60})", it["texte"])
            agenda.append(avec_detail({"titre": it["titre"], "date": futures[0].isoformat(), "heure": it["heure"],
                                       "lieu": propre(lieu.group(1)) if lieu else "", "source": "le-houlme.fr",
                                       "url": it["url"]}))
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

    try:
        transport = collecter_transport()
    except Exception as e:
        transport = None
        avertissements.append(f"Info trafic Astuce illisible : {e}")

    if not ok:
        return None
    return {
        "commune": "Le Houlme",
        "transport": transport,
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
        "magasins": [{"id": m["id"], "nom": m["nom"]} for m in MAGASINS] + [dict(LIDL), dict(ACTION)],
        "catalogues_vus": ancien.get("catalogues_vus", []),
        # promos Leclerc seulement : celles de Lidl et d'Action sont relues à part et ajoutées à la fin
        "promos": [p for p in ancien.get("promos", [])
                   if not set(p.get("magasins", [])) & {ACTION["id"], LIDL["id"]}],
        "lidl_catalogues_vus": ancien.get("lidl_catalogues_vus", []),
        "recettes": ancien.get("recettes", []),
        "ville": ancien.get("ville", {}),
    }

    # magasins Leclerc retirés de la liste (ex. Bapeaume) : leurs promos disparaissent
    suivis = {m["id"] for m in MAGASINS}
    gardees = []
    for p in data["promos"]:
        p["magasins"] = [m for m in p.get("magasins", []) if m in suivis]
        if p["magasins"]:
            gardees.append(p)
    data["promos"] = gardees
    data["catalogues_vus"] = [c for c in data["catalogues_vus"]
                              if set(c.get("magasins", [])) & suivis]
    for c in data["catalogues_vus"]:
        c["magasins"] = [m for m in c.get("magasins", []) if m in suivis]

    print("Promos…")
    catalogues = collecter_promos()
    vus = sorted([{"numero": c["numero"], "titre": c.get("titre", ""), "url": c.get("url", ""),
                   "magasins": sorted(c["magasins"]), "du": c["du"], "au": c["au"]}
                  for c in catalogues.values()], key=lambda c: c["numero"])
    anciens_nums = sorted(c.get("numero", "") for c in data["catalogues_vus"])
    # promos enregistrées avec les codes bruts de l'API (ancienne version du script) : on les relit
    codes_bruts = (any(libelle_remise(p.get("remise", "")) != p.get("remise", "") or p.get("v") != PROMO_VERSION
                       for p in data["promos"])
                   or any("Ã" in c.get("titre", "") for c in data["catalogues_vus"]))
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

    print("Lidl…")
    try:
        lidl, data["lidl_catalogues_vus"] = collecter_lidl(ancien)
    except Exception as e:
        lidl = [p for p in ancien.get("promos", []) if LIDL["id"] in p.get("magasins", [])
                and (not p.get("au") or p["au"] >= AUJOURDHUI.isoformat())]
        avertissements.append(f"Promos Lidl illisibles : {e}")
    data["promos"] = data["promos"] + lidl
    # recettes : avec les promos Leclerc et Lidl (pas Action, c'est surtout du grignotage)
    data["recettes"] = choisir_recettes(data["promos"]) if data["promos"] else data["recettes"]

    print("Action…")
    anciennes_action = [p for p in ancien.get("promos", []) if ACTION["id"] in p.get("magasins", [])
                        and (not p.get("au") or p["au"] >= AUJOURDHUI.isoformat())]
    try:
        action = collecter_action()
    except Exception as e:
        action = []
        avertissements.append(f"Affaires Action illisibles : {e}")
    if not action:
        if not any("Action" in a for a in avertissements):
            avertissements.append("Aucune affaire Action lue : celles de la fois précédente sont conservées")
        action = anciennes_action
    data["promos"] = data["promos"] + action

    if OFFRES_INCONNUES:
        DEBUG.mkdir(exist_ok=True)
        (DEBUG / "offres_sans_montant.json").write_text(
            json.dumps(OFFRES_INCONNUES, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print("Ma ville…")
    for it in ancien.get("ville", {}).get("actus", []) + ancien.get("ville", {}).get("agenda", []):
        if it.get("source") == "le-houlme.fr" and it.get("url") and "texte" in it:
            DETAILS_CACHE[it["url"]] = {"texte": it.get("texte", ""), "lieu": it.get("lieu", ""), "image": it.get("image", "")}
    ville = collecter_ville()
    if ville and not ville.get("transport") and ancien.get("ville", {}).get("transport"):
        ville["transport"] = ancien["ville"]["transport"]  # info trafic illisible cette fois : on garde la précédente
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
