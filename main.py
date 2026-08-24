import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ====== CONNEXION À LA BASE DE DONNÉES ======
# En local (ou si aucune variable d'environnement n'est définie) : fichier SQLite.
# Sur Render, on définira DATABASE_URL vers une vraie base PostgreSQL persistante.
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./livraison.db")
if DATABASE_URL.startswith("postgres://"):
    # Render fournit parfois l'URL avec le préfixe "postgres://", mais SQLAlchemy
    # récent exige "postgresql://" — on corrige automatiquement.
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def maintenant():
    return datetime.now(timezone.utc).isoformat()


def minutes_entre(debut: str, fin: str) -> float:
    d = datetime.fromisoformat(debut)
    f = datetime.fromisoformat(fin)
    return round((f - d).total_seconds() / 60, 1)


# ====== POSITIONS APPROXIMATIVES DES RÉSIDENCES (pour l'itinéraire) ======
RESIDENCES_COORDS = {
    1: (8.6, 3.9), 2: (9.0, 6.6), 3: (6.5, 7.0), 4: (5.5, 8.4),
    5: (10.3, 11.3), 6: (9.9, 16.0), 7: (11.9, 16.1), 8: (7.4, 16.1),
    9: (7.4, 17.4), 10: (8.2, 19.7), 11: (4.0, 16.6), 12: (3.3, 17.8),
    13: (2.1, 19.0), 14: (3.0, 12.3), 15: (13.9, 16.9), 16: (8.8, 21.5),
}
DEPOT_RESIDENCE = 3
MAX_COMMANDES_LIVREUR = 3


def distance(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


# ====== MODÈLES DE TABLES (SQLAlchemy) ======
class ProduitDB(Base):
    __tablename__ = "produits"
    id = Column(Integer, primary_key=True)
    nom = Column(String, nullable=False)
    prix = Column(Float, nullable=False)
    quantite_stock = Column(Integer, default=0)
    seuil_alerte = Column(Integer, default=5)
    prix_achat_moyen = Column(Float, default=0)


class LivreurDB(Base):
    __tablename__ = "livreurs"
    id = Column(Integer, primary_key=True)
    nom = Column(String, nullable=False)
    identifiant = Column(String, unique=True, nullable=False)
    mot_de_passe = Column(String, nullable=False)
    nb_commandes_en_cours = Column(Integer, default=0)


class CompteGerantDB(Base):
    __tablename__ = "compte_gerant"
    id = Column(Integer, primary_key=True)
    identifiant = Column(String, unique=True, nullable=False)
    mot_de_passe = Column(String, nullable=False)
    nom = Column(String, nullable=False)


class CommandeDB(Base):
    __tablename__ = "commandes"
    id = Column(Integer, primary_key=True)
    produit_id = Column(Integer)
    qte = Column(Integer, nullable=False)
    residence = Column(Integer, nullable=False)
    numero_chambre = Column(String, nullable=True)
    adresse = Column(String, nullable=False)
    statut = Column(String, default="en_attente")
    livreur_id = Column(Integer, nullable=True)
    cout_unitaire = Column(Float, default=0)
    date_creation = Column(String)
    date_assignation = Column(String, nullable=True)
    date_depart = Column(String, nullable=True)
    date_livraison = Column(String, nullable=True)


class ReapprovisionnementDB(Base):
    __tablename__ = "reapprovisionnements"
    id = Column(Integer, primary_key=True)
    produit_id = Column(Integer)
    quantite = Column(Integer, nullable=False)
    cout_total = Column(Float, nullable=False)
    date = Column(String)


class ConfigDB(Base):
    __tablename__ = "config"
    id = Column(Integer, primary_key=True)
    mode_assignation = Column(String, default="manuel")


Base.metadata.create_all(bind=engine)


def seed_si_vide():
    """Ajoute les données de départ UNE SEULE FOIS (si la base est neuve/vide)."""
    db = SessionLocal()
    try:
        if db.query(ProduitDB).count() == 0:
            db.add_all([
                ProduitDB(nom="Riz sauce arachide", prix=1500, quantite_stock=20, seuil_alerte=5, prix_achat_moyen=900),
                ProduitDB(nom="Poulet braisé", prix=2500, quantite_stock=8, seuil_alerte=5, prix_achat_moyen=1500),
                ProduitDB(nom="Attiéké poisson", prix=2000, quantite_stock=3, seuil_alerte=5, prix_achat_moyen=1200),
            ])
        if db.query(LivreurDB).count() == 0:
            db.add_all([
                LivreurDB(nom="Karim", identifiant="karim", mot_de_passe="1234", nb_commandes_en_cours=0),
                LivreurDB(nom="Fatou", identifiant="fatou", mot_de_passe="1234", nb_commandes_en_cours=0),
            ])
        if db.query(CompteGerantDB).count() == 0:
            db.add(CompteGerantDB(identifiant="gerant", mot_de_passe="admin", nom="Le gérant"))
        if db.query(ConfigDB).count() == 0:
            db.add(ConfigDB(mode_assignation="manuel"))
        db.commit()
    finally:
        db.close()


seed_si_vide()

app = FastAPI(title="Livraison Cité - API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ====== SÉRIALISATION (objet DB -> dict JSON) ======
def d_produit(p): return {"id": p.id, "nom": p.nom, "prix": p.prix, "quantite_stock": p.quantite_stock,
                           "seuil_alerte": p.seuil_alerte, "prix_achat_moyen": p.prix_achat_moyen}

def d_livreur(l): return {"id": l.id, "nom": l.nom, "identifiant": l.identifiant,
                           "mot_de_passe": l.mot_de_passe, "nb_commandes_en_cours": l.nb_commandes_en_cours}

def d_commande(c): return {"id": c.id, "produit_id": c.produit_id, "qte": c.qte, "residence": c.residence,
                            "numero_chambre": c.numero_chambre, "adresse": c.adresse, "statut": c.statut,
                            "livreur_id": c.livreur_id, "cout_unitaire": c.cout_unitaire,
                            "date_creation": c.date_creation, "date_assignation": c.date_assignation,
                            "date_depart": c.date_depart, "date_livraison": c.date_livraison}

def d_reappro(r): return {"id": r.id, "produit_id": r.produit_id, "quantite": r.quantite,
                           "cout_total": r.cout_total, "date": r.date}


# ====== MODÈLES DE REQUÊTE (Pydantic) ======
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
def login(data: LoginRequest, db: Session = Depends(get_db)):
    if data.role == "gerant":
        compte = db.query(CompteGerantDB).filter_by(identifiant=data.identifiant, mot_de_passe=data.mot_de_passe).first()
        if not compte:
            raise HTTPException(401, "Identifiant ou mot de passe incorrect")
        return {"role": "gerant", "nom": compte.nom}

    livreur = db.query(LivreurDB).filter_by(identifiant=data.identifiant, mot_de_passe=data.mot_de_passe).first()
    if not livreur:
        raise HTTPException(401, "Identifiant ou mot de passe incorrect")
    return {"role": "livreur", "nom": livreur.nom, "livreur_id": livreur.id}


@app.patch("/compte-gerant/mot-de-passe")
def changer_mot_de_passe_gerant(data: ChangerMotDePasseGerant, db: Session = Depends(get_db)):
    compte = db.query(CompteGerantDB).first()
    if data.mot_de_passe_actuel != compte.mot_de_passe:
        raise HTTPException(401, "Mot de passe actuel incorrect")
    if len(data.nouveau_mot_de_passe) < 4:
        raise HTTPException(400, "Le nouveau mot de passe doit faire au moins 4 caractères")
    compte.mot_de_passe = data.nouveau_mot_de_passe
    db.commit()
    return {"ok": True}


# ====== PRODUITS ======
@app.get("/produits")
def get_produits(db: Session = Depends(get_db)):
    return [d_produit(p) for p in db.query(ProduitDB).all()]

@app.post("/produits")
def creer_produit(data: NouveauProduit, db: Session = Depends(get_db)):
    p = ProduitDB(nom=data.nom.strip(), prix=data.prix, quantite_stock=data.quantite_stock,
                  seuil_alerte=data.seuil_alerte, prix_achat_moyen=0)
    db.add(p); db.commit(); db.refresh(p)
    return d_produit(p)

@app.patch("/produits/{produit_id}")
def modifier_produit(produit_id: int, data: ModifierProduit, db: Session = Depends(get_db)):
    p = db.query(ProduitDB).get(produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    if data.nom is not None: p.nom = data.nom.strip()
    if data.prix is not None: p.prix = data.prix
    if data.seuil_alerte is not None: p.seuil_alerte = data.seuil_alerte
    db.commit()
    return d_produit(p)

@app.delete("/produits/{produit_id}")
def supprimer_produit(produit_id: int, db: Session = Depends(get_db)):
    p = db.query(ProduitDB).get(produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    db.delete(p); db.commit()
    return {"ok": True}

@app.patch("/produits/{produit_id}/stock")
def update_stock(produit_id: int, data: StockUpdate, db: Session = Depends(get_db)):
    p = db.query(ProduitDB).get(produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    p.quantite_stock = max(0, p.quantite_stock + data.delta)
    db.commit()
    return d_produit(p)


# ====== RÉAPPROVISIONNEMENT ======
@app.get("/reapprovisionnements")
def get_reapprovisionnements(db: Session = Depends(get_db)):
    return [d_reappro(r) for r in db.query(ReapprovisionnementDB).all()]

@app.post("/reapprovisionnements")
def creer_reapprovisionnement(data: Reapprovisionnement, db: Session = Depends(get_db)):
    p = db.query(ProduitDB).get(data.produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    if data.quantite <= 0 or data.cout_total <= 0:
        raise HTTPException(400, "Quantité et coût doivent être positifs")

    cout_unitaire_ajout = data.cout_total / data.quantite
    stock_avant = p.quantite_stock
    ancien_moyen = p.prix_achat_moyen
    total = stock_avant + data.quantite
    nouveau_moyen = ((stock_avant * ancien_moyen + data.quantite * cout_unitaire_ajout) / total) if total > 0 else cout_unitaire_ajout

    p.quantite_stock += data.quantite
    p.prix_achat_moyen = round(nouveau_moyen, 2)

    r = ReapprovisionnementDB(produit_id=data.produit_id, quantite=data.quantite, cout_total=data.cout_total, date=maintenant())
    db.add(r); db.commit(); db.refresh(r)
    return d_reappro(r)


# ====== LIVREURS ======
@app.get("/livreurs")
def get_livreurs(db: Session = Depends(get_db)):
    return [d_livreur(l) for l in db.query(LivreurDB).all()]

@app.post("/livreurs")
def creer_livreur(data: NouveauLivreur, db: Session = Depends(get_db)):
    identifiant = data.identifiant.strip().lower()
    if db.query(LivreurDB).filter_by(identifiant=identifiant).first():
        raise HTTPException(400, "Cet identifiant est déjà utilisé")
    l = LivreurDB(nom=data.nom.strip(), identifiant=identifiant, mot_de_passe=data.mot_de_passe, nb_commandes_en_cours=0)
    db.add(l); db.commit(); db.refresh(l)
    return d_livreur(l)

@app.patch("/livreurs/{livreur_id}")
def modifier_livreur(livreur_id: int, data: ModifierLivreur, db: Session = Depends(get_db)):
    l = db.query(LivreurDB).get(livreur_id)
    if not l:
        raise HTTPException(404, "Livreur introuvable")
    if data.identifiant is not None:
        nouvel_identifiant = data.identifiant.strip().lower()
        existe = db.query(LivreurDB).filter(LivreurDB.identifiant == nouvel_identifiant, LivreurDB.id != livreur_id).first()
        if existe:
            raise HTTPException(400, "Cet identifiant est déjà utilisé")
        l.identifiant = nouvel_identifiant
    if data.nom is not None:
        l.nom = data.nom.strip()
    if data.mot_de_passe:
        l.mot_de_passe = data.mot_de_passe
    db.commit()
    return d_livreur(l)

@app.delete("/livreurs/{livreur_id}")
def supprimer_livreur(livreur_id: int, db: Session = Depends(get_db)):
    l = db.query(LivreurDB).get(livreur_id)
    if not l:
        raise HTTPException(404, "Livreur introuvable")
    if l.nb_commandes_en_cours > 0:
        raise HTTPException(400, "Ce livreur a des commandes en cours — impossible de le supprimer")
    db.delete(l); db.commit()
    return {"ok": True}


# ====== CONFIG ======
@app.get("/config")
def get_config(db: Session = Depends(get_db)):
    return {"mode_assignation": db.query(ConfigDB).first().mode_assignation}

@app.put("/config")
def set_config(data: ModeConfig, db: Session = Depends(get_db)):
    cfg = db.query(ConfigDB).first()
    cfg.mode_assignation = data.mode_assignation
    db.commit()
    return {"mode_assignation": cfg.mode_assignation}


# ====== COMMANDES ======
@app.get("/commandes")
def get_commandes(db: Session = Depends(get_db)):
    return [d_commande(c) for c in db.query(CommandeDB).all()]


def assigner_commandes_en_attente(db: Session):
    cfg = db.query(ConfigDB).first()
    if cfg.mode_assignation != "auto":
        return
    en_attente = db.query(CommandeDB).filter_by(statut="en_attente").order_by(CommandeDB.id).all()
    for c in en_attente:
        dispo = db.query(LivreurDB).filter(LivreurDB.nb_commandes_en_cours < MAX_COMMANDES_LIVREUR).first()
        if not dispo:
            break
        c.livreur_id = dispo.id
        c.statut = "assignee"
        c.date_assignation = maintenant()
        dispo.nb_commandes_en_cours += 1
    db.commit()


@app.post("/commandes")
def creer_commande(data: NouvelleCommande, db: Session = Depends(get_db)):
    p = db.query(ProduitDB).get(data.produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    if data.qte > p.quantite_stock:
        raise HTTPException(400, "Stock insuffisant")

    p.quantite_stock -= data.qte

    chambre = data.numero_chambre.strip() if data.numero_chambre else None
    c = CommandeDB(
        produit_id=data.produit_id, qte=data.qte, residence=data.residence, numero_chambre=chambre,
        adresse=f"Résidence {data.residence}" + (f", Chambre {chambre}" if chambre else ""),
        statut="en_attente", livreur_id=None, cout_unitaire=p.prix_achat_moyen,
        date_creation=maintenant(), date_assignation=None, date_depart=None, date_livraison=None,
    )
    db.add(c); db.commit(); db.refresh(c)

    assigner_commandes_en_attente(db)
    db.refresh(c)
    return d_commande(c)


@app.post("/commandes/{commande_id}/assigner")
def assigner_commande(commande_id: int, data: AssignationLivreur, db: Session = Depends(get_db)):
    c = db.query(CommandeDB).get(commande_id)
    l = db.query(LivreurDB).get(data.livreur_id)
    if not c or not l:
        raise HTTPException(404, "Commande ou livreur introuvable")
    if l.nb_commandes_en_cours >= MAX_COMMANDES_LIVREUR:
        raise HTTPException(400, "Ce livreur a déjà trop de commandes en cours")

    c.livreur_id = l.id
    c.statut = "assignee"
    c.date_assignation = maintenant()
    l.nb_commandes_en_cours += 1
    db.commit()
    return d_commande(c)


@app.post("/commandes/{commande_id}/demarrer")
def demarrer_livraison(commande_id: int, db: Session = Depends(get_db)):
    c = db.query(CommandeDB).get(commande_id)
    if not c:
        raise HTTPException(404, "Commande introuvable")
    if c.statut != "assignee":
        raise HTTPException(400, "Cette commande n'est pas au statut 'assignée'")
    c.statut = "en_livraison"
    c.date_depart = maintenant()
    db.commit()
    return d_commande(c)


@app.post("/commandes/{commande_id}/livrer")
def marquer_livree(commande_id: int, db: Session = Depends(get_db)):
    c = db.query(CommandeDB).get(commande_id)
    if not c:
        raise HTTPException(404, "Commande introuvable")
    c.statut = "livree"
    c.date_livraison = maintenant()
    if c.livreur_id:
        l = db.query(LivreurDB).get(c.livreur_id)
        if l:
            l.nb_commandes_en_cours -= 1
    db.commit()

    assigner_commandes_en_attente(db)
    db.refresh(c)
    return d_commande(c)


# ====== ITINÉRAIRE LIVREUR ======
@app.get("/livreurs/{livreur_id}/itineraire")
def itineraire_livreur(livreur_id: int, db: Session = Depends(get_db)):
    actives = db.query(CommandeDB).filter(
        CommandeDB.livreur_id == livreur_id,
        CommandeDB.statut.in_(["assignee", "en_livraison"])
    ).all()

    restant = list(actives)
    position = RESIDENCES_COORDS[DEPOT_RESIDENCE]
    ordre = []
    while restant:
        plus_proche = min(restant, key=lambda c: distance(position, RESIDENCES_COORDS.get(c.residence, position)))
        ordre.append(plus_proche)
        position = RESIDENCES_COORDS.get(plus_proche.residence, position)
        restant.remove(plus_proche)

    return [{"ordre": i + 1, "commande_id": c.id, "residence": c.residence} for i, c in enumerate(ordre)]


# ====== STATISTIQUES ======
@app.get("/stats/commandes")
def stats_commandes(db: Session = Depends(get_db)):
    commandes = db.query(CommandeDB).all()
    produits = {p.id: p for p in db.query(ProduitDB).all()}

    par_jour, par_produit, par_adresse, durees = {}, {}, {}, []
    for c in commandes:
        jour = c.date_creation[:10]
        par_jour.setdefault(jour, {"commandes": 0, "volume": 0})
        par_jour[jour]["commandes"] += 1
        par_jour[jour]["volume"] += c.qte

        nom = produits[c.produit_id].nom if c.produit_id in produits else "Produit supprimé"
        par_produit[nom] = par_produit.get(nom, 0) + c.qte

        par_adresse.setdefault(c.adresse, {"commandes": 0, "volume": 0})
        par_adresse[c.adresse]["commandes"] += 1
        par_adresse[c.adresse]["volume"] += c.qte

        if c.statut == "livree" and c.date_livraison:
            durees.append(minutes_entre(c.date_creation, c.date_livraison))

    return {
        "total_commandes": len(commandes),
        "par_jour": par_jour,
        "par_produit": par_produit,
        "par_adresse": par_adresse,
        "temps_traitement_moyen_minutes": round(sum(durees) / len(durees), 1) if durees else None,
    }


@app.get("/stats/finance")
def stats_finance(db: Session = Depends(get_db)):
    produits = {p.id: p for p in db.query(ProduitDB).all()}
    livrees = db.query(CommandeDB).filter_by(statut="livree").all()
    reappros = db.query(ReapprovisionnementDB).all()

    total_revenu, total_cout_ventes = 0.0, 0.0
    transactions = []

    for c in livrees:
        produit = produits.get(c.produit_id)
        prix_vente = produit.prix if produit else 0
        revenu = prix_vente * c.qte
        cout = c.cout_unitaire * c.qte
        total_revenu += revenu
        total_cout_ventes += cout
        transactions.append({"type": "vente", "date": c.date_livraison, "produit": produit.nom if produit else "?",
                              "qte": c.qte, "montant": revenu, "cout": round(cout, 2), "benefice": round(revenu - cout, 2)})

    total_cout_reappro = 0.0
    for r in reappros:
        produit = produits.get(r.produit_id)
        total_cout_reappro += r.cout_total
        transactions.append({"type": "reapprovisionnement", "date": r.date, "produit": produit.nom if produit else "?",
                              "qte": r.quantite, "montant": -r.cout_total, "cout": r.cout_total, "benefice": None})

    transactions.sort(key=lambda t: t["date"], reverse=True)

    return {
        "total_revenu": total_revenu,
        "total_cout_ventes": round(total_cout_ventes, 2),
        "total_cout_reapprovisionnement": total_cout_reappro,
        "benefice_brut": round(total_revenu - total_cout_ventes, 2),
        "transactions": transactions,
    }


@app.get("/stats/livreurs")
def stats_livreurs(db: Session = Depends(get_db)):
    resultat = []
    for l in db.query(LivreurDB).all():
        livrees = db.query(CommandeDB).filter_by(livreur_id=l.id, statut="livree").all()
        durees = [
            minutes_entre(c.date_depart or c.date_assignation, c.date_livraison)
            for c in livrees if (c.date_depart or c.date_assignation) and c.date_livraison
        ]
        resultat.append({
            "livreur_id": l.id, "nom": l.nom, "nb_colis_livres": len(livrees),
            "temps_livraison_moyen_minutes": round(sum(durees) / len(durees), 1) if durees else None,
        })
    return resultat
