from datetime import datetime, timezone
import math
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Livraison Cité - API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def maintenant():
    return datetime.now(timezone.utc).isoformat()

def minutes_entre(debut: str, fin: str) -> float:
    d = datetime.fromisoformat(debut)
    f = datetime.fromisoformat(fin)
    return round((f - d).total_seconds() / 60, 1)

# Positions approximatives des résidences (estimées depuis le plan de la cité).
# Les unités n'ont pas d'importance réelle (pas des mètres exacts) : elles servent
# uniquement à calculer quelle résidence est la plus proche de laquelle, pour ordonner
# la tournée du livreur (algorithme du plus proche voisin).
RESIDENCES_COORDS = {
    1: (870, 375),   2: (910, 650),   3: (650, 700),   4: (555, 845),
    5: (1030, 1145), 6: (1000, 1550), 7: (1195, 1595), 8: (750, 1610),
    9: (750, 1740),  10: (825, 1955), 11: (400, 1610), 12: (325, 1745),
    13: (220, 1885), 14: (300, 1220), 15: (1400, 1745), 16: (870, 2130),
}
DEPOT_RESIDENCE = 3  # point de départ des livreurs

def distance(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


# ====== "BASE DE DONNÉES" EN MÉMOIRE ======
produits = [
    {"id": 1, "nom": "Riz sauce arachide", "prix": 1500, "quantite_stock": 20, "seuil_alerte": 5, "prix_achat_moyen": 900},
    {"id": 2, "nom": "Poulet braisé", "prix": 2500, "quantite_stock": 8, "seuil_alerte": 5, "prix_achat_moyen": 1500},
    {"id": 3, "nom": "Attiéké poisson", "prix": 2000, "quantite_stock": 3, "seuil_alerte": 5, "prix_achat_moyen": 1200},
]
livreurs = [
    {"id": 1, "nom": "Karim", "identifiant": "karim", "mot_de_passe": "1234", "nb_commandes_en_cours": 0},
    {"id": 2, "nom": "Fatou", "identifiant": "fatou", "mot_de_passe": "1234", "nb_commandes_en_cours": 0},
]
comptes_gerant = [
    {"identifiant": "gerant", "mot_de_passe": "admin", "nom": "Le gérant"}
]
commandes = []          # { id, produit_id, qte, adresse, statut, livreur_id, cout_unitaire, date_creation, date_assignation, date_livraison }
reapprovisionnements = []  # { id, produit_id, quantite, cout_total, date }
config = {"mode_assignation": "manuel"}
MAX_COMMANDES_LIVREUR = 3
next_id = 1
next_reappro_id = 1
next_livreur_id = 3
next_produit_id = 4


# ====== MODÈLES ======
class LoginRequest(BaseModel):
    role: str
    identifiant: str
    mot_de_passe: str

class NouvelleCommande(BaseModel):
    produit_id: int
    qte: int
    residence: int
    numero_chambre: str | None = None

class AssignationLivreur(BaseModel):
    livreur_id: int

class ModeConfig(BaseModel):
    mode_assignation: str

class StockUpdate(BaseModel):
    delta: int

class Reapprovisionnement(BaseModel):
    produit_id: int
    quantite: int
    cout_total: float

class NouveauLivreur(BaseModel):
    nom: str
    identifiant: str
    mot_de_passe: str

class ModifierLivreur(BaseModel):
    nom: str | None = None
    identifiant: str | None = None
    mot_de_passe: str | None = None

class ChangerMotDePasseGerant(BaseModel):
    mot_de_passe_actuel: str
    nouveau_mot_de_passe: str

class NouveauProduit(BaseModel):
    nom: str
    prix: float
    seuil_alerte: int = 5
    quantite_stock: int = 0

class ModifierProduit(BaseModel):
    nom: str | None = None
    prix: float | None = None
    seuil_alerte: int | None = None


# ====== AUTHENTIFICATION ======
@app.post("/login")
def login(data: LoginRequest):
    if data.role == "gerant":
        compte = next(
            (c for c in comptes_gerant
             if c["identifiant"] == data.identifiant and c["mot_de_passe"] == data.mot_de_passe),
            None
        )
        if not compte:
            raise HTTPException(401, "Identifiant ou mot de passe incorrect")
        return {"role": "gerant", "nom": compte["nom"]}

    livreur = next(
        (l for l in livreurs
         if l["identifiant"] == data.identifiant and l["mot_de_passe"] == data.mot_de_passe),
        None
    )
    if not livreur:
        raise HTTPException(401, "Identifiant ou mot de passe incorrect")
    return {"role": "livreur", "nom": livreur["nom"], "livreur_id": livreur["id"]}


@app.patch("/compte-gerant/mot-de-passe")
def changer_mot_de_passe_gerant(data: ChangerMotDePasseGerant):
    compte = comptes_gerant[0]
    if data.mot_de_passe_actuel != compte["mot_de_passe"]:
        raise HTTPException(401, "Mot de passe actuel incorrect")
    if len(data.nouveau_mot_de_passe) < 4:
        raise HTTPException(400, "Le nouveau mot de passe doit faire au moins 4 caractères")
    compte["mot_de_passe"] = data.nouveau_mot_de_passe
    return {"ok": True}


# ====== PRODUITS ======
@app.get("/produits")
def get_produits():
    return produits

@app.post("/produits")
def creer_produit(data: NouveauProduit):
    global next_produit_id
    produit = {
        "id": next_produit_id,
        "nom": data.nom.strip(),
        "prix": data.prix,
        "quantite_stock": data.quantite_stock,
        "seuil_alerte": data.seuil_alerte,
        "prix_achat_moyen": 0,
    }
    next_produit_id += 1
    produits.append(produit)
    return produit

@app.patch("/produits/{produit_id}")
def modifier_produit(produit_id: int, data: ModifierProduit):
    produit = next((p for p in produits if p["id"] == produit_id), None)
    if not produit:
        raise HTTPException(404, "Produit introuvable")
    if data.nom is not None:
        produit["nom"] = data.nom.strip()
    if data.prix is not None:
        produit["prix"] = data.prix
    if data.seuil_alerte is not None:
        produit["seuil_alerte"] = data.seuil_alerte
    return produit

@app.delete("/produits/{produit_id}")
def supprimer_produit(produit_id: int):
    produit = next((p for p in produits if p["id"] == produit_id), None)
    if not produit:
        raise HTTPException(404, "Produit introuvable")
    produits.remove(produit)
    return {"ok": True}

@app.patch("/produits/{produit_id}/stock")
def update_stock(produit_id: int, data: StockUpdate):
    produit = next((p for p in produits if p["id"] == produit_id), None)
    if not produit:
        raise HTTPException(404, "Produit introuvable")
    produit["quantite_stock"] = max(0, produit["quantite_stock"] + data.delta)
    return produit


# ====== RÉAPPROVISIONNEMENT (achat de stock) ======
@app.get("/reapprovisionnements")
def get_reapprovisionnements():
    return reapprovisionnements

@app.post("/reapprovisionnements")
def creer_reapprovisionnement(data: Reapprovisionnement):
    global next_reappro_id
    produit = next((p for p in produits if p["id"] == data.produit_id), None)
    if not produit:
        raise HTTPException(404, "Produit introuvable")
    if data.quantite <= 0 or data.cout_total <= 0:
        raise HTTPException(400, "Quantité et coût doivent être positifs")

    cout_unitaire_ajout = data.cout_total / data.quantite

    # Prix d'achat moyen pondéré : on mélange l'ancien stock (à son ancien coût moyen)
    # avec le nouveau lot (à son coût), pour obtenir un nouveau coût moyen.
    stock_avant = produit["quantite_stock"]
    ancien_moyen = produit["prix_achat_moyen"]
    nouveau_moyen = (
        (stock_avant * ancien_moyen + data.quantite * cout_unitaire_ajout)
        / (stock_avant + data.quantite)
    ) if (stock_avant + data.quantite) > 0 else cout_unitaire_ajout

    produit["quantite_stock"] += data.quantite
    produit["prix_achat_moyen"] = round(nouveau_moyen, 2)

    record = {
        "id": next_reappro_id,
        "produit_id": data.produit_id,
        "quantite": data.quantite,
        "cout_total": data.cout_total,
        "date": maintenant(),
    }
    next_reappro_id += 1
    reapprovisionnements.append(record)
    return record


# ====== LIVREURS ======
@app.get("/livreurs")
def get_livreurs():
    return livreurs

@app.post("/livreurs")
def creer_livreur(data: NouveauLivreur):
    global next_livreur_id
    identifiant = data.identifiant.strip().lower()
    if any(l["identifiant"] == identifiant for l in livreurs):
        raise HTTPException(400, "Cet identifiant est déjà utilisé")

    livreur = {
        "id": next_livreur_id,
        "nom": data.nom.strip(),
        "identifiant": identifiant,
        "mot_de_passe": data.mot_de_passe,
        "nb_commandes_en_cours": 0,
    }
    next_livreur_id += 1
    livreurs.append(livreur)
    return livreur

@app.patch("/livreurs/{livreur_id}")
def modifier_livreur(livreur_id: int, data: ModifierLivreur):
    livreur = next((l for l in livreurs if l["id"] == livreur_id), None)
    if not livreur:
        raise HTTPException(404, "Livreur introuvable")

    if data.identifiant is not None:
        nouvel_identifiant = data.identifiant.strip().lower()
        if any(l["identifiant"] == nouvel_identifiant and l["id"] != livreur_id for l in livreurs):
            raise HTTPException(400, "Cet identifiant est déjà utilisé")
        livreur["identifiant"] = nouvel_identifiant
    if data.nom is not None:
        livreur["nom"] = data.nom.strip()
    if data.mot_de_passe is not None and data.mot_de_passe != "":
        livreur["mot_de_passe"] = data.mot_de_passe

    return livreur

@app.delete("/livreurs/{livreur_id}")
def supprimer_livreur(livreur_id: int):
    livreur = next((l for l in livreurs if l["id"] == livreur_id), None)
    if not livreur:
        raise HTTPException(404, "Livreur introuvable")
    if livreur["nb_commandes_en_cours"] > 0:
        raise HTTPException(400, "Ce livreur a des commandes en cours — impossible de le supprimer")

    livreurs.remove(livreur)
    return {"ok": True}


# ====== CONFIG ======
@app.get("/config")
def get_config():
    return config

@app.put("/config")
def set_config(data: ModeConfig):
    config["mode_assignation"] = data.mode_assignation
    return config


# ====== COMMANDES ======
@app.get("/commandes")
def get_commandes():
    return commandes

def assigner_commandes_en_attente():
    """Tant qu'il y a une commande en_attente ET un livreur disponible, on assigne.
    Appelée à la création d'une commande ET après chaque livraison — pour rattraper
    les commandes restées en_attente faute de livreur libre au moment de leur création."""
    if config["mode_assignation"] != "auto":
        return
    for c in commandes:
        if c["statut"] != "en_attente":
            continue
        dispo = next((l for l in livreurs if l["nb_commandes_en_cours"] < MAX_COMMANDES_LIVREUR), None)
        if not dispo:
            break
        c["livreur_id"] = dispo["id"]
        c["statut"] = "assignee"
        c["date_assignation"] = maintenant()
        dispo["nb_commandes_en_cours"] += 1

@app.post("/commandes")
def creer_commande(data: NouvelleCommande):
    global next_id
    produit = next((p for p in produits if p["id"] == data.produit_id), None)
    if not produit:
        raise HTTPException(404, "Produit introuvable")
    if data.qte > produit["quantite_stock"]:
        raise HTTPException(400, "Stock insuffisant")
    if data.residence not in RESIDENCES_COORDS:
        raise HTTPException(400, "Résidence inconnue")

    produit["quantite_stock"] -= data.qte

    chambre = data.numero_chambre.strip() if data.numero_chambre else None
    commande = {
        "id": next_id,
        "produit_id": data.produit_id,
        "qte": data.qte,
        "residence": data.residence,
        "numero_chambre": chambre,
        "adresse": f"Résidence {data.residence}" + (f", Chambre {chambre}" if chambre else ""),
        "statut": "en_attente",
        "livreur_id": None,
        # Coût figé au moment de la vente : le bénéfice ne bouge plus après coup
        # même si le coût moyen du produit change ensuite.
        "cout_unitaire": produit["prix_achat_moyen"],
        "date_creation": maintenant(),
        "date_assignation": None,
        "date_depart": None,
        "date_livraison": None,
    }
    next_id += 1
    commandes.append(commande)

    assigner_commandes_en_attente()

    return commande

@app.post("/commandes/{commande_id}/assigner")
def assigner_commande(commande_id: int, data: AssignationLivreur):
    commande = next((c for c in commandes if c["id"] == commande_id), None)
    livreur = next((l for l in livreurs if l["id"] == data.livreur_id), None)
    if not commande or not livreur:
        raise HTTPException(404, "Commande ou livreur introuvable")
    if livreur["nb_commandes_en_cours"] >= MAX_COMMANDES_LIVREUR:
        raise HTTPException(400, "Ce livreur a déjà trop de commandes en cours")

    commande["livreur_id"] = livreur["id"]
    commande["statut"] = "assignee"
    commande["date_assignation"] = maintenant()
    livreur["nb_commandes_en_cours"] += 1
    return commande

@app.post("/commandes/{commande_id}/demarrer")
def demarrer_livraison(commande_id: int):
    commande = next((c for c in commandes if c["id"] == commande_id), None)
    if not commande:
        raise HTTPException(404, "Commande introuvable")
    if commande["statut"] != "assignee":
        raise HTTPException(400, "Cette commande n'est pas en attente de départ")
    commande["statut"] = "en_livraison"
    commande["date_depart"] = maintenant()
    return commande

@app.post("/commandes/{commande_id}/livrer")
def marquer_livree(commande_id: int):
    commande = next((c for c in commandes if c["id"] == commande_id), None)
    if not commande:
        raise HTTPException(404, "Commande introuvable")
    commande["statut"] = "livree"
    commande["date_livraison"] = maintenant()
    livreur = next((l for l in livreurs if l["id"] == commande["livreur_id"]), None)
    if livreur:
        livreur["nb_commandes_en_cours"] -= 1

    # Un livreur vient de se libérer : on regarde si une commande en_attente peut lui être donnée.
    assigner_commandes_en_attente()

    return commande


# ====== ITINÉRAIRE (plus proche voisin, départ = Résidence 3) ======
@app.get("/livreurs/{livreur_id}/itineraire")
def itineraire_livreur(livreur_id: int):
    livreur = next((l for l in livreurs if l["id"] == livreur_id), None)
    if not livreur:
        raise HTTPException(404, "Livreur introuvable")

    a_livrer = [c for c in commandes if c["livreur_id"] == livreur_id and c["statut"] in ("assignee", "en_livraison")]
    if not a_livrer:
        return []

    position = RESIDENCES_COORDS[DEPOT_RESIDENCE]
    restantes = a_livrer[:]
    ordre = []
    while restantes:
        plus_proche = min(restantes, key=lambda c: distance(position, RESIDENCES_COORDS.get(c["residence"], position)))
        ordre.append(plus_proche)
        position = RESIDENCES_COORDS.get(plus_proche["residence"], position)
        restantes.remove(plus_proche)

    resultat = []
    for i, c in enumerate(ordre):
        produit = next((p for p in produits if p["id"] == c["produit_id"]), None)
        resultat.append({
            "ordre": i + 1,
            "commande_id": c["id"],
            "residence": c["residence"],
            "produit": produit["nom"] if produit else "?",
            "qte": c["qte"],
            "statut": c["statut"],
        })
    return resultat


# ====== STATISTIQUES (gérant) ======
@app.get("/stats/commandes")
def stats_commandes():
    par_jour = {}       # date -> {commandes, volume}
    par_produit = {}    # nom -> volume
    par_adresse = {}    # adresse -> {commandes, volume}
    durees = []

    for c in commandes:
        jour = c["date_creation"][:10]  # "2026-08-22T10:00:00" -> "2026-08-22"
        par_jour.setdefault(jour, {"commandes": 0, "volume": 0})
        par_jour[jour]["commandes"] += 1
        par_jour[jour]["volume"] += c["qte"]

        produit = next((p for p in produits if p["id"] == c["produit_id"]), None)
        nom = produit["nom"] if produit else "Produit supprimé"
        par_produit[nom] = par_produit.get(nom, 0) + c["qte"]

        par_adresse.setdefault(c["adresse"], {"commandes": 0, "volume": 0})
        par_adresse[c["adresse"]]["commandes"] += 1
        par_adresse[c["adresse"]]["volume"] += c["qte"]

        if c["statut"] == "livree" and c["date_livraison"]:
            durees.append(minutes_entre(c["date_creation"], c["date_livraison"]))

    return {
        "total_commandes": len(commandes),
        "par_jour": par_jour,
        "par_produit": par_produit,
        "par_adresse": par_adresse,
        "temps_traitement_moyen_minutes": round(sum(durees) / len(durees), 1) if durees else None,
    }


# ====== FINANCES (gérant) ======
@app.get("/stats/finance")
def stats_finance():
    livrees = [c for c in commandes if c["statut"] == "livree"]

    total_revenu = 0.0
    total_cout_ventes = 0.0
    transactions = []

    for c in livrees:
        produit = next((p for p in produits if p["id"] == c["produit_id"]), None)
        prix_vente = produit["prix"] if produit else 0
        revenu = prix_vente * c["qte"]
        cout = c["cout_unitaire"] * c["qte"]
        total_revenu += revenu
        total_cout_ventes += cout
        transactions.append({
            "type": "vente",
            "date": c["date_livraison"],
            "produit": produit["nom"] if produit else "?",
            "qte": c["qte"],
            "montant": revenu,
            "cout": round(cout, 2),
            "benefice": round(revenu - cout, 2),
        })

    total_cout_reappro = 0.0
    for r in reapprovisionnements:
        produit = next((p for p in produits if p["id"] == r["produit_id"]), None)
        total_cout_reappro += r["cout_total"]
        transactions.append({
            "type": "reapprovisionnement",
            "date": r["date"],
            "produit": produit["nom"] if produit else "?",
            "qte": r["quantite"],
            "montant": -r["cout_total"],
            "cout": r["cout_total"],
            "benefice": None,
        })

    transactions.sort(key=lambda t: t["date"], reverse=True)

    return {
        "total_revenu": total_revenu,
        "total_cout_ventes": round(total_cout_ventes, 2),
        "total_cout_reapprovisionnement": total_cout_reappro,
        "benefice_brut": round(total_revenu - total_cout_ventes, 2),
        "transactions": transactions,
    }


# ====== STATS LIVREURS ======
@app.get("/stats/livreurs")
def stats_livreurs():
    resultat = []
    for l in livreurs:
        livrees = [c for c in commandes if c["livreur_id"] == l["id"] and c["statut"] == "livree"]
        durees = [
            minutes_entre(c["date_depart"] or c["date_assignation"], c["date_livraison"])
            for c in livrees if (c["date_depart"] or c["date_assignation"]) and c["date_livraison"]
        ]
        resultat.append({
            "livreur_id": l["id"],
            "nom": l["nom"],
            "nb_colis_livres": len(livrees),
            "temps_livraison_moyen_minutes": round(sum(durees) / len(durees), 1) if durees else None,
        })
    return resultat
