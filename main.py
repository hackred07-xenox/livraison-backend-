from datetime import datetime, timezone
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


# ====== MODÈLES ======
class LoginRequest(BaseModel):
    role: str
    identifiant: str
    mot_de_passe: str

class NouvelleCommande(BaseModel):
    produit_id: int
    qte: int
    adresse: str

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


# ====== PRODUITS ======
@app.get("/produits")
def get_produits():
    return produits

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

def assigner_auto(commande: dict):
    dispo = next((l for l in livreurs if l["nb_commandes_en_cours"] < MAX_COMMANDES_LIVREUR), None)
    if dispo:
        commande["livreur_id"] = dispo["id"]
        commande["statut"] = "en_livraison"
        commande["date_assignation"] = maintenant()
        dispo["nb_commandes_en_cours"] += 1

@app.post("/commandes")
def creer_commande(data: NouvelleCommande):
    global next_id
    produit = next((p for p in produits if p["id"] == data.produit_id), None)
    if not produit:
        raise HTTPException(404, "Produit introuvable")
    if data.qte > produit["quantite_stock"]:
        raise HTTPException(400, "Stock insuffisant")

    produit["quantite_stock"] -= data.qte

    commande = {
        "id": next_id,
        "produit_id": data.produit_id,
        "qte": data.qte,
        "adresse": data.adresse,
        "statut": "en_attente",
        "livreur_id": None,
        # Coût figé au moment de la vente : le bénéfice ne bouge plus après coup
        # même si le coût moyen du produit change ensuite.
        "cout_unitaire": produit["prix_achat_moyen"],
        "date_creation": maintenant(),
        "date_assignation": None,
        "date_livraison": None,
    }
    next_id += 1
    commandes.append(commande)

    if config["mode_assignation"] == "auto":
        assigner_auto(commande)

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
    commande["statut"] = "en_livraison"
    commande["date_assignation"] = maintenant()
    livreur["nb_commandes_en_cours"] += 1
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
    return commande


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
            minutes_entre(c["date_assignation"], c["date_livraison"])
            for c in livrees if c["date_assignation"] and c["date_livraison"]
        ]
        resultat.append({
            "livreur_id": l["id"],
            "nom": l["nom"],
            "nb_colis_livres": len(livrees),
            "temps_livraison_moyen_minutes": round(sum(durees) / len(durees), 1) if durees else None,
        })
    return resultat
