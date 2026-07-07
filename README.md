# E-NOVAR — Backend

API REST + WebSocket de la plateforme E-NOVAR. Développée avec FastAPI et SQLModel. Déployée sur **Railway**.

---

## Stack technique

| Couche | Technologie |
|---|---|
| Framework | FastAPI 0.115 |
| ORM | SQLModel (SQLAlchemy 2 + Pydantic) |
| Base de données | PostgreSQL via **Supabase** |
| Cache / Pub-Sub | Redis 7 |
| Tâches async | Celery 5 (workers email, PDF, SMS, notifs) |
| Auth | Supabase JWT + bcrypt + TOTP (admin) |
| IA | Anthropic Claude API |
| Paiements | Stripe |
| Notifications | OneSignal (push + email) |
| SMS | Twilio |
| Fichiers | Supabase Storage |
| Langage | Python 3.12 |
| Déploiement | Railway (Dockerfile + nixpacks) |

---

## Prérequis locaux

- **Python** 3.12
- **Docker** + **Docker Compose** (pour Redis)
- Un projet **Supabase** (schéma : `docs/database-schema.sql`)
- (Optionnel) clés Anthropic, Stripe, Twilio, OneSignal pour les fonctionnalités concernées

---

## Installation locale

### 1. Cloner et créer l'environnement Python

```bash
git clone <repo-url>
cd ENOVAR-BACK

python3.12 -m venv .venv
source .venv/bin/activate       # Windows : .venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Variables d'environnement

```bash
cp .env.example .env            # si le fichier n'existe pas encore, créer .env manuellement
```

Remplir `.env` (voir section Variables ci-dessous). Le fichier est ignoré par git.

### 3. Lancer Redis (via Docker)

```bash
docker compose up redis -d
```

Redis écoute sur `localhost:6379`.

### 4. Lancer l'API

```bash
uvicorn app.main:app --reload --port 8000
```

L'API est disponible sur `http://localhost:8000`.  
Documentation interactive : `http://localhost:8000/docs`

### 5. (Optionnel) Lancer les workers Celery

Les workers traitent les emails, PDF, SMS et notifications en arrière-plan.

```bash
# Worker principal
celery -A app.workers.celery_app worker --loglevel=info

# Planificateur de tâches périodiques
celery -A app.workers.celery_app beat --loglevel=info
```

### 6. Tout lancer avec Docker Compose

Pour reproduire l'environnement complet (api + worker + beat + redis) :

```bash
docker compose up --build
```

---

## Variables d'environnement

Créer un fichier `.env` à la racine du projet. Sur Railway, ces variables sont injectées directement dans l'onglet **Variables** du service.

```env
# ── Supabase ───────────────────────────────────────────────────────────────────
# Dashboard → Settings → API
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_ANON_KEY=<anon-key>
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
SUPABASE_JWT_SECRET=<jwt-secret>        # Settings → API → JWT Settings → JWT Secret

# Dashboard → Settings → Database → URI connection string
DATABASE_URL=postgresql://postgres:<password>@db.<project-ref>.supabase.co:5432/postgres

# ── Redis ──────────────────────────────────────────────────────────────────────
# Local : redis://localhost:6379/0
# Railway Redis add-on : copier REDIS_URL depuis les Variables du service Redis
REDIS_URL=redis://localhost:6379/0

# ── Admin (compte privilégié) ──────────────────────────────────────────────────
# Générer ces valeurs avec : python scripts/generate_admin_credentials.py
ADMIN_EMAIL=admin@e-novar.com
ADMIN_PASSWORD_HASH=<bcrypt-hash>
ADMIN_2FA_SECRET=<base32-totp-secret>
ADMIN_JWT_SECRET=<random-256-bit-hex>
ADMIN_JWT_EXPIRE_MINUTES=60

# ── App ────────────────────────────────────────────────────────────────────────
SECRET_KEY=<random-secret>
APP_ENV=development                      # "production" sur Railway
APP_URL=http://localhost:8000
FRONTEND_URL=http://localhost:5173
ALLOWED_ORIGINS=http://localhost:5173    # virgule-séparé pour plusieurs origines

# ── IA (Anthropic Claude) ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-...
AI_FREE_DAILY_QUOTA=10                   # 0 = illimité

# ── Stripe ────────────────────────────────────────────────────────────────────
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...

# ── OneSignal (push + email) ───────────────────────────────────────────────────
ONESIGNAL_APP_ID=<app-id>
ONESIGNAL_REST_API_KEY=<rest-key>

# ── Twilio (SMS) ───────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+213...

# ── Resend (email transactionnel) ─────────────────────────────────────────────
RESEND_API_KEY=re_...
EMAIL_FROM=noreply@enovar.dz

# ── Supabase Storage ──────────────────────────────────────────────────────────
SUPABASE_STORAGE_BUCKET=enovar-files
```

---

## Provisionner le compte admin

Le compte admin est entièrement géré par variables d'environnement (pas de table dédiée en DB).  
Pour générer les credentials :

```bash
# Installer les dépendances du script si nécessaire
pip install bcrypt pyotp qrcode

# Lancer le script interactif
python scripts/generate_admin_credentials.py
```

Le script demande email + mot de passe, génère le hash bcrypt, un secret TOTP, et un JWT secret.  
Il affiche également un QR code à scanner dans Google Authenticator / Microsoft Authenticator / Authy.  
Copier les valeurs affichées dans les Variables Railway (ou dans `.env` en local).

---

## Base de données & migrations

Le schéma complet est dans `docs/database-schema.sql`. Il est géré **manuellement** via le SQL Editor de Supabase (pas d'Alembic en production).

Les migrations incrémentales sont dans `docs/migrations/` et numérotées séquentiellement :

```
docs/migrations/
├── 001_add_availability_to_student_profiles.sql
├── 002_complete_onboarding_data.sql
├── ...
└── 039_promo_system_v2.sql
```

**Pour appliquer une migration :**
1. Ouvrir **Supabase Dashboard → SQL Editor**
2. Coller et exécuter le contenu du fichier `.sql` concerné

> Appliquer les migrations dans l'ordre numérique. Ne jamais modifier un fichier déjà appliqué en production : créer un nouveau fichier numéroté à la suite.

---

## Organisation du code

```
app/
├── main.py                    # point d'entrée FastAPI : routers, CORS, WebSocket, lifespan
├── config.py                  # Settings (pydantic-settings) — lit exclusivement les env vars
├── database.py                # engine SQLModel + get_db session factory
├── dependencies.py            # FastAPI Depends : get_current_user, get_admin_user, get_db
│
├── core/
│   ├── connections.py         # connexions Supabase (client JS et service role)
│   ├── redis.py               # pool Redis asyncio
│   ├── security.py            # vérification JWT Supabase + admin JWT
│   └── exceptions.py          # handlers d'exceptions globaux
│
├── models/                    # SQLModel — chaque fichier = un domaine
│   ├── enums.py               # tous les Enum partagés (KpSource, ReferralStatus…)
│   ├── user.py                # Profile (= auth.users mirror)
│   ├── teacher.py             # TeacherProfile, TeacherDocument
│   ├── booking.py             # Booking
│   ├── kp.py                  # KpAccount, KpTransaction, KpLevel
│   ├── gamification.py        # Badge, StudentBadge, Challenge, ChallengeSubmission
│   ├── referral.py            # ReferralCode, ReferralLink
│   ├── admin.py               # PromoCode, PromoRedemption, AuditLog, Report…
│   ├── message.py             # Conversation, Message
│   ├── notification.py        # Notification
│   ├── homework.py            # Homework, HomeworkSubmission
│   ├── evaluation.py          # Evaluation
│   ├── practice.py            # PracticeAttempt, Question
│   ├── session.py             # Session (séance)
│   ├── store.py               # StoreItem, Purchase, Effect
│   ├── catalog.py             # Subject, Level, Wilaya
│   ├── ai.py                  # AIConversation, AIMessage
│   └── …
│
├── schemas/                   # Pydantic — requêtes/réponses API (séparés des modèles DB)
│   ├── auth.py
│   ├── booking.py
│   ├── teacher.py
│   ├── admin.py               # PromoCodeCreate, PromoCodeUpdate…
│   ├── kp.py
│   ├── challenge.py
│   ├── homework.py
│   ├── message.py
│   ├── notification.py
│   └── …
│
├── routers/                   # endpoints FastAPI (un fichier = un domaine)
│   ├── auth.py                # /api/auth — login, register, refresh, OAuth Google
│   ├── profile.py             # /api/profile
│   ├── teachers.py            # /api/teachers — recherche publique
│   ├── bookings.py            # /api/bookings
│   ├── sessions.py            # /api/sessions
│   ├── kp.py                  # /api/kp — solde, historique, niveaux
│   ├── challenges.py          # /api/challenges — liste, soumission preuves
│   ├── referrals.py           # /api/referrals — code, appliquer, stats
│   ├── promos.py              # /api/promos — codes promo (vue étudiant)
│   ├── homework.py            # /api/homework
│   ├── messages.py            # /api/messages + WebSocket /ws/chat/{room}
│   ├── notifications.py       # /api/notifications
│   ├── store.py               # /api/store
│   ├── student_dashboard.py   # /api/student/dashboard
│   ├── student_teachers.py    # /api/student/teachers
│   ├── student_badges.py      # /api/student/badges
│   ├── student_leaderboard.py # /api/student/leaderboard
│   ├── student_progress.py    # /api/student/progress
│   ├── student_practice.py    # /api/student/practice — quiz
│   ├── student_homework.py    # /api/student/homework
│   ├── ai.py                  # /api/ai — assistant Claude
│   ├── files.py               # /api/files — upload Supabase Storage
│   ├── favorites.py           # /api/favorites
│   ├── onboarding.py          # /api/onboarding
│   ├── payments.py            # /api/payments — Stripe
│   ├── parent.py              # /api/parent
│   ├── catalogs.py            # /api/catalogs — matières, niveaux, wilayas
│   │
│   └── admin/                 # /api/admin — routes protégées admin (TOTP JWT)
│       ├── auth.py            # /api/admin/auth — login admin 2FA
│       ├── stats.py           # /api/admin/stats — dashboard chiffres
│       ├── teachers.py        # /api/admin/teachers — approbation/suspension
│       ├── users.py           # /api/admin/users
│       ├── reviews.py         # /api/admin/reviews
│       ├── challenges.py      # /api/admin/challenges — CRUD défis
│       ├── questions.py       # /api/admin/questions — banque de questions
│       ├── store.py           # /api/admin/store — items boutique
│       ├── promos.py          # /api/admin/promos — CRUD codes promo + analytics
│       ├── referrals.py       # /api/admin/referrals — statistiques parrainage
│       ├── content.py         # /api/admin/content — CMS pages légales
│       └── messages.py        # /api/admin/messages — modération
│
├── services/                  # logique métier réutilisable
│   ├── kp.py                  # award_kp(), spend_kp(), gestion niveaux
│   ├── referral.py            # validate_referral_for_user() — déclenchement récompenses
│   ├── auth.py                # vérification Supabase, refresh token
│   ├── badge_engine.py        # attribution automatique des badges
│   ├── effects.py             # EP boost (items boutique)
│   ├── notification.py        # création et envoi de notifications
│   ├── onesignal.py           # client OneSignal (push + email)
│   ├── storage.py             # upload/delete Supabase Storage
│   ├── store.py               # achat d'items, activation d'effets
│   ├── stripe.py              # création sessions de paiement
│   └── ai.py                  # streaming Claude, gestion quota
│
└── workers/                   # tâches Celery asynchrones
    ├── celery_app.py          # configuration Celery (broker Redis)
    ├── email_tasks.py         # envoi d'emails transactionnels
    ├── notification_tasks.py  # notifications push OneSignal
    ├── pdf_tasks.py           # génération de PDF (factures, rapports)
    └── sms_tasks.py           # SMS Twilio
```

---

## Flux d'authentification

### Utilisateurs (étudiants, enseignants, parents)

1. Le frontend appelle `POST /api/auth/login` avec email + mot de passe
2. Le backend vérifie les credentials auprès de **Supabase Auth**
3. Supabase retourne un `access_token` (JWT) + `refresh_token`
4. Le frontend stocke les tokens dans `localStorage`
5. Chaque requête API envoie `Authorization: Bearer <access_token>`
6. `get_current_user` (dependency FastAPI) vérifie la signature du JWT avec `SUPABASE_JWT_SECRET`
7. Sur 401, le frontend appelle `/api/auth/refresh` pour renouveler le token

### Admin

1. `POST /api/admin/auth/login` — email + mot de passe bcrypt
2. `POST /api/admin/auth/totp` — code TOTP (Google Authenticator)
3. Réponse : JWT admin signé avec `ADMIN_JWT_SECRET`
4. Routes admin vérifées par `get_admin_user` (dependency distincte)

---

## Déploiement (Railway)

Le déploiement est automatique sur push via CI/CD GitHub Actions → Railway.

```
railway.toml      # configuration Railway (healthcheck, region…)
Dockerfile        # image Python 3.12-slim
nixpacks.toml     # fallback nixpacks si Docker non utilisé
Procfile          # commandes de démarrage (web + worker)
```

**Variables requises sur Railway** : toutes celles listées dans la section Variables ci-dessus.  
Les ajouter dans : **Service → Variables** (elles sont injectées dans l'environnement du container).
