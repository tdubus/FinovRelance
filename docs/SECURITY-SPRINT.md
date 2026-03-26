# Securite — Sprint Import Excel/CSV

Audit realise le 2026-03-26. Couvre le processus complet d'import fichiers.

---

## P1 — A corriger dans le prochain sprint

### 1. Rate limiting sur les routes d'import + guard concurrent jobs

**Fichiers:** `views/company_views.py` (routes `file_import_*`)
**Risque:** DoS — un utilisateur peut lancer des centaines d'imports concurrents dans la limite globale (5000/jour).

**Action:**
- Ajouter `@limiter.limit("10 per hour")` sur les 4 routes d'import
- Ajouter un guard contre les jobs concurrents avant de creer un nouveau job :
```python
running = ImportJob.query.filter_by(company_id=company.id, status='processing').first()
if running:
    flash('Un import est deja en cours. Veuillez patienter.', 'warning')
    return redirect(...)
```

### 2. Ownership check sur le SSE progress endpoint

**Fichier:** `import_progress.py:325`
**Risque:** Fuite d'information — tout utilisateur authentifie qui obtient un UUID de session peut observer l'import d'une autre entreprise (nombre de clients/factures).

**Action:**
- Stocker `company_id` dans la session de progression a la creation
- Verifier dans `stream_import_progress` que `company_id` correspond a l'utilisateur courant

### 3. Sanitize les messages d'exception avant stockage/affichage

**Fichiers:** `import_worker.py:312,324` / `views/company_views.py`
**Risque:** Disclosure — les exceptions SQLAlchemy contiennent noms de tables, contraintes, parfois des valeurs partielles. Elles sont stockees dans `ImportJob.result_message` et affichees a l'utilisateur.

**Action:**
- Logger le `str(e)` complet cote serveur (deja fait via `logger.error`)
- Stocker un message generique dans `result_message` et dans les notifications :
```python
job.mark_as_failed("Erreur interne lors de l'import. Contactez le support si le probleme persiste.")
```

### 4. Bypass limite de licence via import client AJAX

**Fichier:** `views/company_views.py` — `file_import_clients_start` / `_file_import_process`
**Risque:** Un utilisateur sur un plan limite peut contourner sa limite de clients via le path AJAX (qui est celui utilise par le frontend).

**Action:**
- Ajouter `company.assert_client_capacity(new_clients_count)` dans `_file_import_process` avant le bulk insert
- Meme pattern que `file_import_clients` ligne 4720

### 5. ImportJob orphelin en `processing` sur capacity violation

**Fichier:** `views/company_views.py:4595-4723`
**Risque:** Le job est committe `processing` avant le check de capacite. Si la capacite est depassee, redirect sans marquer le job `failed`.

**Action:**
- Deplacer la creation du `ImportJob` apres le check de capacite, ou
- Marquer le job `failed` dans le bloc de redirection capacity

### 6. Import sync client peut depasser le timeout Gunicorn 120s

**Fichiers:** `docker-entrypoint.sh:71` + `views/company_views.py:4541-4918`
**Risque:** `file_import_clients` traite tout de maniere synchrone. Gros fichier = worker tue mid-commit, DB dans un etat partiellement committe.

**Action:**
- Migrer `file_import_clients` vers le pattern async ImportWorker (comme `file_import_invoices`)
- Ou supprimer ce path synchrone si le AJAX path le remplace completement

---

## P2 — Dette technique / hardening

### 7. Unifier les 3 paths client import sur ImportWorker

**Fichiers:** `views/company_views.py` (3 implementations) + `import_worker.py`
**Impact:** Meme pattern que le fix factures : une seule implementation dans ImportWorker, appelee par la route.

Les 3 paths actuels divergent sur :
- Licence check (present seulement dans le POST sync)
- Parent hierarchy `parent_client_id` (present seulement dans le POST sync)
- Language validation whitelist (absent de ImportWorker)
- Strategie bulk update (CASE WHEN vs bulk_update_mappings vs bulk_save_objects)

**Action:**
- Enrichir `ImportWorker._process_clients` avec : licence check, parent hierarchy, language validation
- Brancher `file_import_clients_start` sur ImportWorker (comme on a fait pour invoices)
- Supprimer `file_import_clients` (POST sync) et `_file_import_process`

### 8. Protection ZIP bomb / XML bomb pour fichiers XLSX

**Fichier:** `file_import_connector.py:146,273`
**Impact:** `openpyxl.load_workbook()` ne protege pas contre les XLSX malicieux (ZIP contenant du XML qui explose en memoire).

**Action:**
```python
import zipfile, io
MAX_UNZIPPED = 50 * 1024 * 1024  # 50 MB
with zipfile.ZipFile(io.BytesIO(file_content)) as zf:
    total = sum(i.file_size for i in zf.infolist())
    if total > MAX_UNZIPPED:
        return [], 0, ["Fichier Excel trop volumineux une fois decompresse"]
```

### 9. Fallback encoding CSV (utf-8-sig puis latin-1)

**Fichier:** `file_import_connector.py:174,371`
**Impact:** Les CSV exportes depuis Windows Excel en francais (cp1252/latin-1) crash avec `UnicodeDecodeError`.

**Action:**
```python
try:
    content = file_content.decode('utf-8-sig')
except UnicodeDecodeError:
    content = file_content.decode('latin-1')
```

### 10. `secure_filename()` sur les noms de fichiers uploades

**Fichiers:** `views/company_views.py:4600,5105`
**Impact:** Le `file.filename` brut est stocke en DB. Pas de XSS (Jinja2 echappe) mais audit trail trompeur et suffix temp file derive du nom utilisateur.

**Action:**
```python
from werkzeug.utils import secure_filename
safe_name = secure_filename(file.filename) or 'import_file'
```

### 11. Import inutilise `generate_password_hash` dans ImportWorker

**Fichier:** `import_worker.py:347`
**Action:** Supprimer l'import.

### 12. `parent_code` silencieusement ignore dans ImportWorker

**Fichier:** `import_worker.py:388`
**Impact:** La hierarchie parent-enfant est perdue pour les imports clients via ImportWorker.
**Action:** A traiter dans le cadre de l'unification des paths client (point 7).

### 13. `_process_invoices` charge tous les objets Invoice en memoire sans `load_only`

**Fichier:** `import_worker.py:488-491`
**Action:** Utiliser `load_only` comme le faisait l'ancien code :
```python
from sqlalchemy.orm import load_only
all_invoices = session.query(Invoice).filter_by(company_id=company_id).options(
    load_only(Invoice.id, Invoice.invoice_number, Invoice.amount)
).all()
```
