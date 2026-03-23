# Archives des Politiques Légales

Ce dossier contient les archives historiques des politiques légales de FinovRelance (CGU, Politique de Confidentialité, Politique des Cookies).

## 📋 Conformité RGPD/Loi 25

L'archivage des politiques est obligatoire pour :
- Prouver le consentement des utilisateurs à une version spécifique
- Respecter les obligations de traçabilité RGPD/Loi 25
- Permettre un audit en cas de contrôle

## 📁 Structure des fichiers

Les fichiers archivés suivent le format : `{nom_politique}_{date_version}.html`

Exemples :
- `terms_2025-10-13.html` - CGU du 13 octobre 2025
- `privacy_2025-10-13.html` - Politique de confidentialité du 13 octobre 2025
- `cookies_2025-10-13.html` - Politique des cookies du 13 octobre 2025

## 🔧 Utilisation du système d'archivage

### Archiver une politique avant modification

```python
from utils.policy_archiver import archive_policy

# Archiver avec la date actuelle
archive_policy('cgu')  
archive_policy('confidentialite')
archive_policy('cookies')

# Archiver avec une date spécifique
archive_policy('cgu', '2025-10-13')
```

### Lister les archives

```python
from utils.policy_archiver import list_archives

# Toutes les archives
all_archives = list_archives()

# Archives d'une politique spécifique
cgu_archives = list_archives('cgu')
```

### Restaurer une archive (DANGER)

```python
from utils.policy_archiver import restore_archive

# La restauration nécessite une confirmation explicite
restore_archive('templates/legal/archives/terms_2025-10-13.html', confirm=True)
```

⚠️ **IMPORTANT** : La restauration archive automatiquement la version actuelle avant de restaurer l'ancienne.

## 🔄 Processus de modification d'une politique

1. **Archiver la version actuelle** :
   ```python
   from utils.policy_archiver import archive_policy
   archive_policy('cgu', '2025-10-13')  # Date de l'ancienne version
   ```

2. **Modifier le fichier** dans `templates/legal/`

3. **Mettre à jour la version** dans `utils/consent_helper.py` :
   ```python
   CURRENT_TERMS_VERSION = "2025-10-20"  # Nouvelle date
   ```

4. **Forcer le re-consentement** : Les utilisateurs verront le modal de consentement au prochain login

## 📊 Correspondance avec la base de données

Chaque consentement utilisateur dans la table `consent_logs` référence :
- `consent_version` : Version de la politique acceptée (ex: "2025-10-13")
- `created_at` : Date et heure d'acceptation

Cela permet de retrouver exactement quelle version a été acceptée par chaque utilisateur.

## 🛡️ Sécurité

- Les archives ne doivent **jamais** être supprimées
- Seuls les administrateurs système peuvent restaurer des archives
- Toute modification de politique doit être tracée
- Conservation recommandée : 7 ans minimum (conformité juridique)

## 📝 Notes techniques

- Format : HTML (templates Jinja2)
- Encodage : UTF-8
- Compression : Non (pour faciliter l'audit)
- Signature : Optionnelle (hash MD5/SHA256 pour vérification d'intégrité)
