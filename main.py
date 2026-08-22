from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Livraison Cité - API")

# Autorise le frontend (page HTML) à appeler cette API depuis le navigateur.
# En production, on limiterait allow_origins à l'adresse exacte du site.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====== "BASE DE DONNÉES" EN MÉMOIRE ======
# Pour l'instant : des listes Python en mémoire (perdues au redémarrage du serveur).
# Prochaine étape possible : remplacer ça par une vraie base (SQLite).
produits = [
    {"id": 1, "nom": "Riz sauce arachide", "prix": 1500, "quantite_stock": 20, "seuil_alerte": 5},
    {"id": 2, "nom": "Poulet braisé", "prix": 2500, "quantite_stock": 8, "seuil_alerte": 5},
    {"id": 3, "nom": "Attiéké poisson", "prix": 2000, "quantite_stock": 3, "seuil_alerte": 5},
]
livreurs = [
    {"id": 1, "nom": "Karim", "identifiant": "karim", "mot_de_passe": "1234", "nb_commandes_en_cours": 0},
    {"id": 2, "nom": "Fatou", "identifiant": "fatou", "mot_de_passe": "1234", "nb_commandes_en_cours": 0},
]
comptes_gerant = [
    {"identifiant": "gerant", "mot_de_passe": "admin", "nom": "Le gérant"}
]
commandes = []
config = {"mode_assignation": "manuel"}
MAX_COMMANDES_LIVREUR = 3
next_id = 1


# ====== MODÈLES ======
# Un modèle Pydantic décrit la "forme" attendue des données envoyées par le client.
# Si les données envoyées ne correspondent pas, FastAPI refuse automatiquement (erreur 422).
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


# ====== LIVREURS ======
@app.get("/livreurs")
def get_livreurs():
    return livreurs


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
        dispo["nb_commandes_en_cours"] += 1

@app.post("/commandes")
def creer_commande(data: NouvelleCommande):
    global next_id
    produit = next((p for p in produits if p["id"] == data.produit_id), None)
    if not produit:
        raise HTTPException(404, "Produit introuvable")
    if data.qte > produit["quantite_stock"]:
        raise HTTPException(400, "Stock insuffisant")

    # Le stock diminue directement (pas d'étape "préparation" — plats déjà prêts)
    produit["quantite_stock"] -= data.qte

    commande = {
        "id": next_id,
        "produit_id": data.produit_id,
        "qte": data.qte,
        "adresse": data.adresse,
        "statut": "en_attente",
        "livreur_id": None,
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
    livreur["nb_commandes_en_cours"] += 1
    return commande

@app.post("/commandes/{commande_id}/livrer")
def marquer_livree(commande_id: int):
    commande = next((c for c in commandes if c["id"] == commande_id), None)
    if not commande:
        raise HTTPException(404, "Commande introuvable")
    commande["statut"] = "livree"
    livreur = next((l for l in livreurs if l["id"] == commande["livreur_id"]), None)
    if livreur:
        livreur["nb_commandes_en_cours"] -= 1
    return commande
