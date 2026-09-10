import os
import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, inspect, text
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


# ====== SÉCURITÉ : hachage des mots de passe (PBKDF2, sans dépendance externe) ======
PBKDF2_ITERATIONS = 260_000

def hacher_mot_de_passe(mot_de_passe: str) -> str:
    sel = secrets.token_hex(16)
    empreinte = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode(), bytes.fromhex(sel), PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${sel}${empreinte.hex()}"

def est_mot_de_passe_hache(valeur: str) -> bool:
    return isinstance(valeur, str) and valeur.startswith("pbkdf2_sha256$") and valeur.count("$") == 3

def verifier_mot_de_passe(mot_de_passe: str, empreinte_stockee: str) -> bool:
    if not est_mot_de_passe_hache(empreinte_stockee):
        return False
    try:
        _, iterations, sel, hash_attendu = empreinte_stockee.split("$")
        calcul = hashlib.pbkdf2_hmac("sha256", mot_de_passe.encode(), bytes.fromhex(sel), int(iterations))
        return hmac.compare_digest(calcul.hex(), hash_attendu)
    except (ValueError, TypeError):
        return False


# ====== SÉCURITÉ : sessions par token signé (sans dépendance externe type JWT) ======
# IMPORTANT : définir la variable d'environnement SECRET_KEY sur Render avec une valeur secrète
# fixe. Sans elle, une clé aléatoire est générée à chaque redémarrage et déconnecte tout le monde.
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
TOKEN_DUREE_SECONDES = 12 * 3600  # 12h, à ajuster selon le confort d'usage voulu

def emettre_token(role: str, sujet_id: int) -> str:
    charge = {"role": role, "id": sujet_id, "exp": int(time.time()) + TOKEN_DUREE_SECONDES}
    charge_b64 = base64.urlsafe_b64encode(json.dumps(charge, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(SECRET_KEY.encode(), charge_b64.encode(), hashlib.sha256).hexdigest()
    return f"{charge_b64}.{signature}"

def verifier_token(token: str) -> dict | None:
    try:
        charge_b64, signature = token.split(".")
        signature_attendue = hmac.new(SECRET_KEY.encode(), charge_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, signature_attendue):
            return None
        charge = json.loads(base64.urlsafe_b64decode(charge_b64 + "=" * (-len(charge_b64) % 4)))
        if charge.get("exp", 0) < time.time():
            return None
        return charge
    except Exception:
        return None

def utilisateur_courant(authorization: str | None = Header(default=None)) -> dict:
    """Dépendance FastAPI : exige un token valide dans l'en-tête Authorization: Bearer <token>."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Authentification requise")
    charge = verifier_token(authorization.removeprefix("Bearer ").strip())
    if not charge:
        raise HTTPException(401, "Session invalide ou expirée — reconnecte-toi")
    return charge

def exiger_gerant(utilisateur: dict = Depends(utilisateur_courant)) -> dict:
    """Dépendance FastAPI : exige en plus que l'utilisateur connecté soit le gérant."""
    if utilisateur["role"] != "gerant":
        raise HTTPException(403, "Réservé au gérant")
    return utilisateur

def exiger_soi_meme_ou_gerant(livreur_id: int, utilisateur: dict = Depends(utilisateur_courant)) -> dict:
    """Dépendance FastAPI : un livreur ne peut agir que sur ses propres données ; le gérant peut tout voir."""
    if utilisateur["role"] == "livreur" and utilisateur["id"] != livreur_id:
        raise HTTPException(403, "Tu ne peux accéder qu'à tes propres données")
    return utilisateur


# ====== MODÈLES DE TABLES (SQLAlchemy) ======
class ProduitDB(Base):
    __tablename__ = "produits"
    id = Column(Integer, primary_key=True)
    nom = Column(String, nullable=False)
    prix = Column(Float, nullable=False)
    quantite_stock = Column(Integer, default=0)
    seuil_alerte = Column(Integer, default=5)
    prix_achat_moyen = Column(Float, default=0)
    categorie = Column(String, nullable=True)
    actif = Column(Boolean, default=True)


class LivreurDB(Base):
    __tablename__ = "livreurs"
    id = Column(Integer, primary_key=True)
    nom = Column(String, nullable=False)
    identifiant = Column(String, unique=True, nullable=False)
    mot_de_passe = Column(String, nullable=False)
    nb_commandes_en_cours = Column(Integer, default=0)
    taux_par_livraison = Column(Float, default=0)
    disponible = Column(Boolean, default=True)


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
    probleme_motif = Column(String, nullable=True)
    produit_nom = Column(String, nullable=True)


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


class MouvementStockDB(Base):
    """Historique complet de chaque changement de stock, quelle qu'en soit la cause."""
    __tablename__ = "mouvements_stock"
    id = Column(Integer, primary_key=True)
    produit_id = Column(Integer)
    type = Column(String, nullable=False)  # vente / reapprovisionnement / ajustement_manuel / perte
    delta = Column(Integer, nullable=False)  # positif = ajout, négatif = retrait
    motif = Column(String, nullable=True)
    date = Column(String)


class PerteDB(Base):
    """Invendus jetés en fin de journée (les plats ne se gardent pas au lendemain)."""
    __tablename__ = "pertes"
    id = Column(Integer, primary_key=True)
    produit_id = Column(Integer)
    quantite = Column(Integer, nullable=False)
    cout_total = Column(Float, nullable=False)
    date = Column(String)


class PaiementLivreurDB(Base):
    """Rémunération versée à un livreur pour une livraison effectuée."""
    __tablename__ = "paiements_livreurs"
    id = Column(Integer, primary_key=True)
    livreur_id = Column(Integer)
    commande_id = Column(Integer)
    montant = Column(Float, nullable=False)
    date = Column(String)


Base.metadata.create_all(bind=engine)


def migrer_schema():
    """create_all() crée les tables manquantes mais NE MODIFIE JAMAIS une table existante.
    Cette fonction complète : elle vérifie, colonne par colonne, ce qui manque sur les
    tables déjà présentes (ex: après une mise à jour du code) et l'ajoute automatiquement,
    sans toucher aux données déjà enregistrées. À maintenir à chaque nouvelle colonne ajoutée
    à un modèle existant."""
    colonnes_attendues = {
        "produits": [
            ("categorie", "TEXT"),
            ("actif", "BOOLEAN DEFAULT TRUE"),
        ],
        "livreurs": [
            ("taux_par_livraison", "FLOAT DEFAULT 0"),
            ("disponible", "BOOLEAN DEFAULT TRUE"),
        ],
        "commandes": [
            ("probleme_motif", "TEXT"),
            ("produit_nom", "TEXT"),
        ],
    }
    inspecteur = inspect(engine)
    tables_existantes = inspecteur.get_table_names()
    with engine.connect() as connexion:
        for table, colonnes in colonnes_attendues.items():
            if table not in tables_existantes:
                continue  # la table sera créée avec toutes ses colonnes par create_all, rien à faire
            colonnes_existantes = {c["name"] for c in inspecteur.get_columns(table)}
            for nom_colonne, type_sql in colonnes:
                if nom_colonne not in colonnes_existantes:
                    connexion.execute(text(f"ALTER TABLE {table} ADD COLUMN {nom_colonne} {type_sql}"))
                    connexion.commit()


migrer_schema()


def seed_si_vide():
    """Ajoute les données de départ UNE SEULE FOIS (si la base est neuve/vide)."""
    db = SessionLocal()
    try:
        if db.query(ProduitDB).count() == 0:
            produits_depart = [
                ProduitDB(nom="Riz sauce arachide", prix=1500, quantite_stock=20, seuil_alerte=5, prix_achat_moyen=900),
                ProduitDB(nom="Poulet braisé", prix=2500, quantite_stock=8, seuil_alerte=5, prix_achat_moyen=1500),
                ProduitDB(nom="Attiéké poisson", prix=2000, quantite_stock=3, seuil_alerte=5, prix_achat_moyen=1200),
            ]
            db.add_all(produits_depart)
            db.flush()  # pour obtenir les id générés avant de logger les mouvements
            for p in produits_depart:
                db.add(MouvementStockDB(produit_id=p.id, type="ajustement_manuel", delta=p.quantite_stock,
                                         motif="Stock de départ", date=maintenant()))
        if db.query(LivreurDB).count() == 0:
            db.add_all([
                LivreurDB(nom="Karim", identifiant="karim", mot_de_passe=hacher_mot_de_passe("1234"), nb_commandes_en_cours=0),
                LivreurDB(nom="Fatou", identifiant="fatou", mot_de_passe=hacher_mot_de_passe("1234"), nb_commandes_en_cours=0),
            ])
        if db.query(CompteGerantDB).count() == 0:
            db.add(CompteGerantDB(identifiant="gerant", mot_de_passe=hacher_mot_de_passe("admin"), nom="Le gérant"))
        if db.query(ConfigDB).count() == 0:
            db.add(ConfigDB(mode_assignation="manuel"))
        db.commit()
    finally:
        db.close()


seed_si_vide()


def migrer_mots_de_passe_en_clair():
    """Sur une base déjà en production, les mots de passe existants sont encore en clair
    (avant ce correctif de sécurité). On les hache une bonne fois pour toutes au démarrage,
    sans rien casser : les identifiants et mots de passe existants continuent de fonctionner."""
    db = SessionLocal()
    try:
        a_modifie = False
        for compte in db.query(CompteGerantDB).all():
            if not est_mot_de_passe_hache(compte.mot_de_passe):
                compte.mot_de_passe = hacher_mot_de_passe(compte.mot_de_passe)
                a_modifie = True
        for l in db.query(LivreurDB).all():
            if not est_mot_de_passe_hache(l.mot_de_passe):
                l.mot_de_passe = hacher_mot_de_passe(l.mot_de_passe)
                a_modifie = True
        if a_modifie:
            db.commit()
    finally:
        db.close()


migrer_mots_de_passe_en_clair()


def migrer_produit_nom_commandes():
    """Rattrapage pour les commandes déjà en base, créées avant l'ajout du champ produit_nom :
    on renseigne le nom à partir du produit s'il existe encore. Si le produit a depuis été
    supprimé, le nom est définitivement perdu pour cette commande (rien à récupérer) — mais toutes
    les commandes créées à partir de maintenant garderont leur nom même si le produit est supprimé."""
    db = SessionLocal()
    try:
        a_modifie = False
        commandes_sans_nom = db.query(CommandeDB).filter(
            (CommandeDB.produit_nom == None) | (CommandeDB.produit_nom == "")
        ).all()
        for c in commandes_sans_nom:
            p = db.query(ProduitDB).get(c.produit_id)
            if p:
                c.produit_nom = p.nom
                a_modifie = True
        if a_modifie:
            db.commit()
    finally:
        db.close()


migrer_produit_nom_commandes()

app = FastAPI(title="Livraison Cité - API")
# IMPORTANT : remplace "*" par l'origine exacte de ton frontend une fois son URL finale connue
# (ex: ["https://tonapp.pages.dev"]) pour empêcher n'importe quel site d'appeler cette API.
ORIGINES_AUTORISEES = os.environ.get("ORIGINES_AUTORISEES", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ORIGINES_AUTORISEES] if ORIGINES_AUTORISEES != "*" else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def enregistrer_mouvement(db: Session, produit_id: int, type_mouvement: str, delta: int, motif: str = None):
    db.add(MouvementStockDB(produit_id=produit_id, type=type_mouvement, delta=delta, motif=motif, date=maintenant()))


def dans_periode(date_str: str, date_debut: str | None, date_fin: str | None) -> bool:
    jour = date_str[:10]
    if date_debut and jour < date_debut:
        return False
    if date_fin and jour > date_fin:
        return False
    return True


# ====== SÉRIALISATION (objet DB -> dict JSON) ======
def d_produit(p): return {"id": p.id, "nom": p.nom, "prix": p.prix, "quantite_stock": p.quantite_stock,
                           "seuil_alerte": p.seuil_alerte, "prix_achat_moyen": p.prix_achat_moyen,
                           "categorie": p.categorie, "actif": p.actif}

def d_livreur(l): return {"id": l.id, "nom": l.nom, "identifiant": l.identifiant,
                           "nb_commandes_en_cours": l.nb_commandes_en_cours,
                           "taux_par_livraison": l.taux_par_livraison, "disponible": l.disponible}

def d_commande(c): return {"id": c.id, "produit_id": c.produit_id, "produit_nom": c.produit_nom, "qte": c.qte, "residence": c.residence,
                            "numero_chambre": c.numero_chambre,
                            "adresse": c.adresse, "statut": c.statut,
                            "livreur_id": c.livreur_id, "cout_unitaire": c.cout_unitaire,
                            "date_creation": c.date_creation, "date_assignation": c.date_assignation,
                            "date_depart": c.date_depart, "date_livraison": c.date_livraison,
                            "probleme_motif": c.probleme_motif}

def d_reappro(r): return {"id": r.id, "produit_id": r.produit_id, "quantite": r.quantite,
                           "cout_total": r.cout_total, "date": r.date}

def d_mouvement(m): return {"id": m.id, "produit_id": m.produit_id, "type": m.type,
                             "delta": m.delta, "motif": m.motif, "date": m.date}

def d_perte(p): return {"id": p.id, "produit_id": p.produit_id, "quantite": p.quantite,
                         "cout_total": p.cout_total, "date": p.date}

def d_paiement(p): return {"id": p.id, "livreur_id": p.livreur_id, "commande_id": p.commande_id,
                            "montant": p.montant, "date": p.date}


# ====== MODÈLES DE REQUÊTE (Pydantic) ======
class LoginRequest(BaseModel):
    role: str
    identifiant: str
    mot_de_passe: str

class NouvelleCommande(BaseModel):
    produit_id: int
    qte: int
    residence: int
    numero_chambre: str

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
    taux_par_livraison: float = 0

class ModifierLivreur(BaseModel):
    nom: str | None = None
    identifiant: str | None = None
    mot_de_passe: str | None = None
    taux_par_livraison: float | None = None
    disponible: bool | None = None

class Disponibilite(BaseModel):
    disponible: bool

class SignalerProbleme(BaseModel):
    motif: str

class ChangerMotDePasseGerant(BaseModel):
    mot_de_passe_actuel: str
    nouveau_mot_de_passe: str

class NouveauProduit(BaseModel):
    nom: str
    prix: float
    seuil_alerte: int = 5
    quantite_stock: int = 0
    categorie: str | None = None

class ModifierProduit(BaseModel):
    nom: str | None = None
    prix: float | None = None
    seuil_alerte: int | None = None
    categorie: str | None = None
    actif: bool | None = None


# ====== AUTHENTIFICATION ======
@app.post("/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    if data.role == "gerant":
        compte = db.query(CompteGerantDB).filter_by(identifiant=data.identifiant).first()
        if not compte or not verifier_mot_de_passe(data.mot_de_passe, compte.mot_de_passe):
            raise HTTPException(401, "Identifiant ou mot de passe incorrect")
        token = emettre_token(role="gerant", sujet_id=compte.id)
        return {"role": "gerant", "nom": compte.nom, "token": token}

    livreur = db.query(LivreurDB).filter_by(identifiant=data.identifiant).first()
    if not livreur or not verifier_mot_de_passe(data.mot_de_passe, livreur.mot_de_passe):
        raise HTTPException(401, "Identifiant ou mot de passe incorrect")
    token = emettre_token(role="livreur", sujet_id=livreur.id)
    return {"role": "livreur", "nom": livreur.nom, "livreur_id": livreur.id, "token": token}


@app.patch("/compte-gerant/mot-de-passe")
def changer_mot_de_passe_gerant(data: ChangerMotDePasseGerant, db: Session = Depends(get_db),
                                 utilisateur: dict = Depends(exiger_gerant)):
    compte = db.query(CompteGerantDB).first()
    if not verifier_mot_de_passe(data.mot_de_passe_actuel, compte.mot_de_passe):
        raise HTTPException(401, "Mot de passe actuel incorrect")
    if len(data.nouveau_mot_de_passe) < 4:
        raise HTTPException(400, "Le nouveau mot de passe doit faire au moins 4 caractères")
    compte.mot_de_passe = hacher_mot_de_passe(data.nouveau_mot_de_passe)
    db.commit()
    return {"ok": True}


# ====== PRODUITS ======
@app.get("/produits")
def get_produits(db: Session = Depends(get_db), utilisateur: dict = Depends(utilisateur_courant)):
    return [d_produit(p) for p in db.query(ProduitDB).all()]

@app.post("/produits")
def creer_produit(data: NouveauProduit, db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    p = ProduitDB(nom=data.nom.strip(), prix=data.prix, quantite_stock=data.quantite_stock,
                  seuil_alerte=data.seuil_alerte, prix_achat_moyen=0,
                  categorie=(data.categorie.strip() if data.categorie else None), actif=True)
    db.add(p); db.commit(); db.refresh(p)
    if data.quantite_stock > 0:
        enregistrer_mouvement(db, p.id, "ajustement_manuel", data.quantite_stock, motif="Stock de départ")
        db.commit()
    return d_produit(p)

@app.patch("/produits/{produit_id}")
def modifier_produit(produit_id: int, data: ModifierProduit, db: Session = Depends(get_db),
                      utilisateur: dict = Depends(exiger_gerant)):
    p = db.query(ProduitDB).get(produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    if data.nom is not None: p.nom = data.nom.strip()
    if data.prix is not None: p.prix = data.prix
    if data.seuil_alerte is not None: p.seuil_alerte = data.seuil_alerte
    if data.categorie is not None: p.categorie = data.categorie.strip() or None
    if data.actif is not None: p.actif = data.actif
    db.commit()
    return d_produit(p)

@app.delete("/produits/{produit_id}")
def supprimer_produit(produit_id: int, db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    p = db.query(ProduitDB).get(produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    db.delete(p); db.commit()
    return {"ok": True}

@app.patch("/produits/{produit_id}/stock")
def update_stock(produit_id: int, data: StockUpdate, db: Session = Depends(get_db),
                  utilisateur: dict = Depends(exiger_gerant)):
    p = db.query(ProduitDB).get(produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    ancien = p.quantite_stock
    p.quantite_stock = max(0, p.quantite_stock + data.delta)
    delta_reel = p.quantite_stock - ancien
    if delta_reel != 0:
        enregistrer_mouvement(db, p.id, "ajustement_manuel", delta_reel)
    db.commit()
    return d_produit(p)


# ====== HISTORIQUE DES MOUVEMENTS DE STOCK ======
@app.get("/mouvements-stock")
def get_mouvements_stock(produit_id: int | None = None, db: Session = Depends(get_db),
                          utilisateur: dict = Depends(exiger_gerant)):
    q = db.query(MouvementStockDB)
    if produit_id is not None:
        q = q.filter_by(produit_id=produit_id)
    return [d_mouvement(m) for m in q.order_by(MouvementStockDB.id.desc()).all()]


# ====== RÉAPPROVISIONNEMENT ======
@app.get("/reapprovisionnements")
def get_reapprovisionnements(db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    return [d_reappro(r) for r in db.query(ReapprovisionnementDB).all()]

@app.post("/reapprovisionnements")
def creer_reapprovisionnement(data: Reapprovisionnement, db: Session = Depends(get_db),
                               utilisateur: dict = Depends(exiger_gerant)):
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
    db.add(r)
    enregistrer_mouvement(db, data.produit_id, "reapprovisionnement", data.quantite)
    db.commit(); db.refresh(r)
    return d_reappro(r)


# ====== LIVREURS ======
@app.get("/livreurs")
def get_livreurs(db: Session = Depends(get_db), utilisateur: dict = Depends(utilisateur_courant)):
    return [d_livreur(l) for l in db.query(LivreurDB).all()]

@app.post("/livreurs")
def creer_livreur(data: NouveauLivreur, db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    identifiant = data.identifiant.strip().lower()
    if db.query(LivreurDB).filter_by(identifiant=identifiant).first():
        raise HTTPException(400, "Cet identifiant est déjà utilisé")
    l = LivreurDB(nom=data.nom.strip(), identifiant=identifiant, mot_de_passe=hacher_mot_de_passe(data.mot_de_passe),
                  nb_commandes_en_cours=0, taux_par_livraison=data.taux_par_livraison)
    db.add(l); db.commit(); db.refresh(l)
    return d_livreur(l)

@app.patch("/livreurs/{livreur_id}")
def modifier_livreur(livreur_id: int, data: ModifierLivreur, db: Session = Depends(get_db),
                      utilisateur: dict = Depends(exiger_gerant)):
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
        l.mot_de_passe = hacher_mot_de_passe(data.mot_de_passe)
    if data.taux_par_livraison is not None:
        l.taux_par_livraison = data.taux_par_livraison
    if data.disponible is not None:
        l.disponible = data.disponible
    db.commit()
    return d_livreur(l)

@app.post("/livreurs/{livreur_id}/disponibilite")
def changer_disponibilite(livreur_id: int, data: Disponibilite, db: Session = Depends(get_db),
                           utilisateur: dict = Depends(exiger_soi_meme_ou_gerant)):
    """Le livreur signale lui-même s'il est disponible ou en pause — l'assignation auto en tient compte."""
    l = db.query(LivreurDB).get(livreur_id)
    if not l:
        raise HTTPException(404, "Livreur introuvable")
    l.disponible = data.disponible
    db.commit()
    return d_livreur(l)

@app.delete("/livreurs/{livreur_id}")
def supprimer_livreur(livreur_id: int, db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    l = db.query(LivreurDB).get(livreur_id)
    if not l:
        raise HTTPException(404, "Livreur introuvable")
    if l.nb_commandes_en_cours > 0:
        raise HTTPException(400, "Ce livreur a des commandes en cours — impossible de le supprimer")
    db.delete(l); db.commit()
    return {"ok": True}


# ====== CONFIG ======
@app.get("/config")
def get_config(db: Session = Depends(get_db), utilisateur: dict = Depends(utilisateur_courant)):
    return {"mode_assignation": db.query(ConfigDB).first().mode_assignation}

@app.put("/config")
def set_config(data: ModeConfig, db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    cfg = db.query(ConfigDB).first()
    cfg.mode_assignation = data.mode_assignation
    db.commit()
    return {"mode_assignation": cfg.mode_assignation}


# ====== COMMANDES ======
@app.get("/commandes")
def get_commandes(db: Session = Depends(get_db), utilisateur: dict = Depends(utilisateur_courant)):
    return [d_commande(c) for c in db.query(CommandeDB).all()]


def assigner_commandes_en_attente(db: Session):
    cfg = db.query(ConfigDB).first()
    if cfg.mode_assignation != "auto":
        return
    en_attente = db.query(CommandeDB).filter_by(statut="en_attente").order_by(CommandeDB.id).all()
    for c in en_attente:
        dispo = db.query(LivreurDB).filter(
            LivreurDB.nb_commandes_en_cours < MAX_COMMANDES_LIVREUR,
            LivreurDB.disponible == True,
        ).first()
        if not dispo:
            break
        c.livreur_id = dispo.id
        c.statut = "assignee"
        c.date_assignation = maintenant()
        dispo.nb_commandes_en_cours += 1
    db.commit()


@app.post("/commandes")
def creer_commande(data: NouvelleCommande, db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    p = db.query(ProduitDB).get(data.produit_id)
    if not p:
        raise HTTPException(404, "Produit introuvable")
    if data.qte > p.quantite_stock:
        raise HTTPException(400, "Stock insuffisant")

    p.quantite_stock -= data.qte

    chambre = data.numero_chambre.strip()
    if not chambre:
        raise HTTPException(400, "Le numéro de chambre est obligatoire")
    c = CommandeDB(
        produit_id=data.produit_id, produit_nom=p.nom, qte=data.qte, residence=data.residence, numero_chambre=chambre,
        adresse=f"Résidence {data.residence}, Chambre {chambre}",
        statut="en_attente", livreur_id=None, cout_unitaire=p.prix_achat_moyen,
        date_creation=maintenant(), date_assignation=None, date_depart=None, date_livraison=None,
    )
    db.add(c)
    enregistrer_mouvement(db, data.produit_id, "vente", -data.qte)
    db.commit(); db.refresh(c)

    assigner_commandes_en_attente(db)
    db.refresh(c)
    return d_commande(c)


@app.post("/commandes/{commande_id}/assigner")
def assigner_commande(commande_id: int, data: AssignationLivreur, db: Session = Depends(get_db),
                       utilisateur: dict = Depends(exiger_gerant)):
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
def demarrer_livraison(commande_id: int, db: Session = Depends(get_db), utilisateur: dict = Depends(utilisateur_courant)):
    c = db.query(CommandeDB).get(commande_id)
    if not c:
        raise HTTPException(404, "Commande introuvable")
    if utilisateur["role"] == "livreur" and c.livreur_id != utilisateur["id"]:
        raise HTTPException(403, "Cette commande n'est pas assignée à toi")
    if c.statut != "assignee":
        raise HTTPException(400, "Cette commande n'est pas au statut 'assignée'")
    c.statut = "en_livraison"
    c.date_depart = maintenant()
    db.commit()
    return d_commande(c)


@app.post("/commandes/{commande_id}/livrer")
def marquer_livree(commande_id: int, db: Session = Depends(get_db), utilisateur: dict = Depends(utilisateur_courant)):
    c = db.query(CommandeDB).get(commande_id)
    if not c:
        raise HTTPException(404, "Commande introuvable")
    if utilisateur["role"] == "livreur" and c.livreur_id != utilisateur["id"]:
        raise HTTPException(403, "Cette commande n'est pas assignée à toi")
    c.statut = "livree"
    c.date_livraison = maintenant()
    if c.livreur_id:
        l = db.query(LivreurDB).get(c.livreur_id)
        if l:
            l.nb_commandes_en_cours -= 1
            if l.taux_par_livraison > 0:
                db.add(PaiementLivreurDB(livreur_id=l.id, commande_id=c.id, montant=l.taux_par_livraison, date=maintenant()))
    db.commit()

    assigner_commandes_en_attente(db)
    db.refresh(c)
    return d_commande(c)


@app.post("/commandes/{commande_id}/probleme")
def signaler_probleme(commande_id: int, data: SignalerProbleme, db: Session = Depends(get_db),
                       utilisateur: dict = Depends(utilisateur_courant)):
    """Le livreur signale un souci (client absent, adresse introuvable...) : la commande
    retourne au gérant (en_attente) pour être réassignée, et le motif reste visible."""
    c = db.query(CommandeDB).get(commande_id)
    if not c:
        raise HTTPException(404, "Commande introuvable")
    if utilisateur["role"] == "livreur" and c.livreur_id != utilisateur["id"]:
        raise HTTPException(403, "Cette commande n'est pas assignée à toi")
    if c.livreur_id:
        l = db.query(LivreurDB).get(c.livreur_id)
        if l:
            l.nb_commandes_en_cours = max(0, l.nb_commandes_en_cours - 1)
    c.probleme_motif = data.motif
    c.statut = "en_attente"
    c.livreur_id = None
    c.date_assignation = None
    c.date_depart = None
    db.commit()
    db.refresh(c)
    return d_commande(c)


@app.post("/commandes/{commande_id}/annuler")
def annuler_commande(commande_id: int, data: SignalerProbleme, db: Session = Depends(get_db),
                      utilisateur: dict = Depends(utilisateur_courant)):
    """Annulation définitive par le livreur (client injoignable, refuse la commande, erreur...) :
    contrairement à /probleme, la commande n'est PAS réassignée à quelqu'un d'autre. Le plat, déjà
    préparé et décompté du stock à la création de la commande, est comptabilisé en perte plutôt que
    d'être remis en stock (un plat cuisiné ne se reconserve pas)."""
    c = db.query(CommandeDB).get(commande_id)
    if not c:
        raise HTTPException(404, "Commande introuvable")
    if utilisateur["role"] == "livreur" and c.livreur_id != utilisateur["id"]:
        raise HTTPException(403, "Cette commande n'est pas assignée à toi")
    if c.statut in ("livree", "annulee"):
        raise HTTPException(400, "Cette commande est déjà terminée")

    if c.livreur_id:
        l = db.query(LivreurDB).get(c.livreur_id)
        if l:
            l.nb_commandes_en_cours = max(0, l.nb_commandes_en_cours - 1)

    cout_total = round(c.cout_unitaire * c.qte, 2)
    db.add(PerteDB(produit_id=c.produit_id, quantite=c.qte, cout_total=cout_total, date=maintenant()))
    enregistrer_mouvement(db, c.produit_id, "annulation", 0, motif=f"Commande #{c.id} annulée — {data.motif}")

    c.statut = "annulee"
    c.probleme_motif = data.motif
    db.commit()

    assigner_commandes_en_attente(db)  # le livreur libéré peut reprendre une commande en attente
    db.refresh(c)
    return d_commande(c)


# ====== ITINÉRAIRE LIVREUR ======
@app.get("/livreurs/{livreur_id}/itineraire")
def itineraire_livreur(livreur_id: int, db: Session = Depends(get_db),
                        utilisateur: dict = Depends(exiger_soi_meme_ou_gerant)):
    actives = db.query(CommandeDB).filter(
        CommandeDB.livreur_id == livreur_id,
        CommandeDB.statut.in_(["assignee", "en_livraison"])
    ).all()

    # Estimation grossière : les unités de coordonnées ne sont pas de vraies distances GPS,
    # donc ce temps est une approximation relative (utile pour comparer les arrêts entre eux,
    # pas une durée garantie). À affiner quand on aura les vraies coordonnées GPS.
    MINUTES_PAR_UNITE = 3

    restant = list(actives)
    position = RESIDENCES_COORDS[DEPOT_RESIDENCE]
    ordre = []
    while restant:
        plus_proche = min(restant, key=lambda c: distance(position, RESIDENCES_COORDS.get(c.residence, position)))
        d = distance(position, RESIDENCES_COORDS.get(plus_proche.residence, position))
        ordre.append((plus_proche, round(d * MINUTES_PAR_UNITE, 1)))
        position = RESIDENCES_COORDS.get(plus_proche.residence, position)
        restant.remove(plus_proche)

    return [{"ordre": i + 1, "commande_id": c.id, "residence": c.residence, "temps_estime_minutes": t}
            for i, (c, t) in enumerate(ordre)]


# ====== STATISTIQUES ======
@app.get("/stats/commandes")
def stats_commandes(date_debut: str | None = None, date_fin: str | None = None, db: Session = Depends(get_db),
                     utilisateur: dict = Depends(exiger_gerant)):
    commandes = [c for c in db.query(CommandeDB).all() if dans_periode(c.date_creation, date_debut, date_fin)]
    produits = {p.id: p for p in db.query(ProduitDB).all()}

    par_jour, par_produit, par_adresse, par_residence, par_heure = {}, {}, {}, {}, {}
    durees_livraison, durees_attente = [], []
    volume_total = 0

    for c in commandes:
        jour = c.date_creation[:10]
        par_jour.setdefault(jour, {"commandes": 0, "volume": 0})
        par_jour[jour]["commandes"] += 1
        par_jour[jour]["volume"] += c.qte
        volume_total += c.qte

        # Heure de la journée (0-23), pour repérer les pics d'affluence
        heure = c.date_creation[11:13]
        par_heure.setdefault(heure, {"commandes": 0, "volume": 0})
        par_heure[heure]["commandes"] += 1
        par_heure[heure]["volume"] += c.qte

        nom = produits[c.produit_id].nom if c.produit_id in produits else "Produit supprimé"
        par_produit[nom] = par_produit.get(nom, 0) + c.qte

        par_adresse.setdefault(c.adresse, {"commandes": 0, "volume": 0})
        par_adresse[c.adresse]["commandes"] += 1
        par_adresse[c.adresse]["volume"] += c.qte

        cle_residence = f"Résidence {c.residence}"
        par_residence.setdefault(cle_residence, {"commandes": 0, "volume": 0})
        par_residence[cle_residence]["commandes"] += 1
        par_residence[cle_residence]["volume"] += c.qte

        if c.statut == "livree" and c.date_livraison:
            durees_livraison.append(minutes_entre(c.date_creation, c.date_livraison))
        if c.date_assignation:
            durees_attente.append(minutes_entre(c.date_creation, c.date_assignation))

    # Pourcentage du volume total pour chaque résidence
    for d in par_residence.values():
        d["pourcentage"] = round(d["volume"] / volume_total * 100, 1) if volume_total else 0

    return {
        "total_commandes": len(commandes),
        "par_jour": par_jour,
        "par_heure": par_heure,
        "par_produit": par_produit,
        "par_adresse": par_adresse,
        "par_residence": par_residence,
        "temps_traitement_moyen_minutes": round(sum(durees_livraison) / len(durees_livraison), 1) if durees_livraison else None,
        "temps_attente_moyen_minutes": round(sum(durees_attente) / len(durees_attente), 1) if durees_attente else None,
    }


@app.get("/stats/gaspillage")
def stats_gaspillage(db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    """Pour chaque plat : quelle part de ce qui a été approvisionné a fini en perte (invendu jeté)."""
    produits = db.query(ProduitDB).all()
    resultat = []
    for p in produits:
        mouvements = db.query(MouvementStockDB).filter_by(produit_id=p.id).all()
        approvisionne = sum(m.delta for m in mouvements if m.delta > 0)
        perdu = sum(-m.delta for m in mouvements if m.type == "perte")
        taux = round(perdu / approvisionne * 100, 1) if approvisionne > 0 else 0
        resultat.append({
            "produit": p.nom, "quantite_approvisionnee": approvisionne,
            "quantite_perdue": perdu, "taux_gaspillage_pct": taux,
        })
    return sorted(resultat, key=lambda r: r["taux_gaspillage_pct"], reverse=True)


@app.get("/stats/finance")
def stats_finance(date_debut: str | None = None, date_fin: str | None = None, db: Session = Depends(get_db),
                   utilisateur: dict = Depends(exiger_gerant)):
    produits = {p.id: p for p in db.query(ProduitDB).all()}
    livreurs = {l.id: l for l in db.query(LivreurDB).all()}

    livrees = [c for c in db.query(CommandeDB).filter_by(statut="livree").all()
               if dans_periode(c.date_livraison, date_debut, date_fin)]
    reappros = [r for r in db.query(ReapprovisionnementDB).all() if dans_periode(r.date, date_debut, date_fin)]
    pertes = [p for p in db.query(PerteDB).all() if dans_periode(p.date, date_debut, date_fin)]
    paiements = [p for p in db.query(PaiementLivreurDB).all() if dans_periode(p.date, date_debut, date_fin)]

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

    total_cout_pertes = 0.0
    for p in pertes:
        produit = produits.get(p.produit_id)
        total_cout_pertes += p.cout_total
        transactions.append({"type": "perte", "date": p.date, "produit": produit.nom if produit else "?",
                              "qte": p.quantite, "montant": -p.cout_total, "cout": p.cout_total, "benefice": -p.cout_total})

    total_cout_livreurs = 0.0
    for pl in paiements:
        livreur = livreurs.get(pl.livreur_id)
        total_cout_livreurs += pl.montant
        transactions.append({"type": "paiement_livreur", "date": pl.date, "produit": livreur.nom if livreur else "?",
                              "qte": 1, "montant": -pl.montant, "cout": pl.montant, "benefice": -pl.montant})

    transactions.sort(key=lambda t: t["date"], reverse=True)

    benefice_brut = round(total_revenu - total_cout_ventes, 2)
    benefice_net = round(benefice_brut - total_cout_pertes - total_cout_livreurs, 2)

    # Marge en % par produit (sur la base du prix de vente et du coût moyen actuels)
    marges = []
    for p in produits.values():
        if p.prix > 0:
            marges.append({"produit": p.nom, "marge_pct": round((p.prix - p.prix_achat_moyen) / p.prix * 100, 1)})

    # Solde de caisse réel : argent encaissé moins argent réellement décaissé
    # (les pertes ne sont PAS un mouvement de caisse : l'argent a déjà été payé au réapprovisionnement)
    solde_caisse = round(total_revenu - total_cout_reappro - total_cout_livreurs, 2)

    return {
        "total_revenu": total_revenu,
        "total_cout_ventes": round(total_cout_ventes, 2),
        "total_cout_reapprovisionnement": total_cout_reappro,
        "total_cout_pertes": round(total_cout_pertes, 2),
        "total_cout_livreurs": round(total_cout_livreurs, 2),
        "benefice_brut": benefice_brut,
        "benefice_net": benefice_net,
        "solde_caisse": solde_caisse,
        "repartition_depenses": {"vendeuses": total_cout_reappro, "livreurs": round(total_cout_livreurs, 2)},
        "marges": marges,
        "transactions": transactions,
    }


@app.get("/stats/livreurs")
def stats_livreurs(db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
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


# ====== CLÔTURE DE JOURNÉE ======
@app.post("/cloture-journee")
def cloturer_journee(db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    """Les plats ne se gardent pas au lendemain : tout stock restant est une perte sèche.
    On la comptabilise, puis on remet le stock à zéro pour repartir propre le lendemain."""
    aujourdhui = maintenant()[:10]
    produits_avec_stock = db.query(ProduitDB).filter(ProduitDB.quantite_stock > 0).all()

    detail_pertes = []
    total_cout_pertes = 0.0
    for p in produits_avec_stock:
        cout = round(p.quantite_stock * p.prix_achat_moyen, 2)
        db.add(PerteDB(produit_id=p.id, quantite=p.quantite_stock, cout_total=cout, date=maintenant()))
        enregistrer_mouvement(db, p.id, "perte", -p.quantite_stock, motif="Clôture de journée")
        detail_pertes.append({"produit": p.nom, "quantite_perdue": p.quantite_stock, "cout": cout})
        total_cout_pertes += cout
        p.quantite_stock = 0
    db.commit()

    # Petit rapport du jour, pour clôturer avec une vue d'ensemble complète
    commandes_du_jour = [c for c in db.query(CommandeDB).filter_by(statut="livree").all() if c.date_livraison and c.date_livraison[:10] == aujourdhui]
    produits = {p.id: p for p in db.query(ProduitDB).all()}
    revenu_jour = sum((produits[c.produit_id].prix if c.produit_id in produits else 0) * c.qte for c in commandes_du_jour)
    cout_ventes_jour = sum(c.cout_unitaire * c.qte for c in commandes_du_jour)
    paiements_jour = [p for p in db.query(PaiementLivreurDB).all() if p.date[:10] == aujourdhui]
    total_paiements_jour = sum(p.montant for p in paiements_jour)
    benefice_net_jour = round(revenu_jour - cout_ventes_jour - total_cout_pertes - total_paiements_jour, 2)

    return {
        "date": aujourdhui,
        "pertes": detail_pertes,
        "total_cout_pertes": round(total_cout_pertes, 2),
        "revenu_jour": revenu_jour,
        "cout_ventes_jour": round(cout_ventes_jour, 2),
        "paiements_livreurs_jour": round(total_paiements_jour, 2),
        "benefice_net_jour": benefice_net_jour,
    }


@app.get("/pertes")
def get_pertes(db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    return [d_perte(p) for p in db.query(PerteDB).all()]

@app.get("/paiements-livreurs")
def get_paiements_livreurs(db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    return [d_paiement(p) for p in db.query(PaiementLivreurDB).all()]


@app.post("/reinitialiser-activite")
def reinitialiser_activite(db: Session = Depends(get_db), utilisateur: dict = Depends(exiger_gerant)):
    """Vide toutes les données d'ACTIVITÉ (commandes, mouvements, pertes, paiements) —
    utile pour repartir propre après une phase de test. Les plats et livreurs déjà
    configurés (nom, prix, identifiants...) ne sont PAS supprimés, seule leur activité l'est."""
    db.query(CommandeDB).delete()
    db.query(ReapprovisionnementDB).delete()
    db.query(PerteDB).delete()
    db.query(PaiementLivreurDB).delete()
    db.query(MouvementStockDB).delete()

    for p in db.query(ProduitDB).all():
        p.quantite_stock = 0
        p.prix_achat_moyen = 0
    for l in db.query(LivreurDB).all():
        l.nb_commandes_en_cours = 0

    db.commit()
    return {"ok": True}


@app.get("/stats/finance/export-csv")
def export_finance_csv(date_debut: str | None = None, date_fin: str | None = None, db: Session = Depends(get_db),
                        utilisateur: dict = Depends(exiger_gerant)):
    import csv, io
    data = stats_finance(date_debut=date_debut, date_fin=date_fin, db=db, utilisateur=utilisateur)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Date", "Type", "Produit", "Quantité", "Montant (F)", "Coût (F)", "Bénéfice (F)"])
    for t in data["transactions"]:
        writer.writerow([t["date"], t["type"], t["produit"], t["qte"], t["montant"], t["cout"], t["benefice"]])
    return Response(content=buffer.getvalue(), media_type="text/csv",
                     headers={"Content-Disposition": "attachment; filename=transactions.csv"})
