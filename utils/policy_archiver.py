"""
Système d'archivage des politiques légales (CGU, Confidentialité, Cookies)
Conforme RGPD/Loi 25 - Historique des versions
"""

import shutil
from datetime import datetime
from pathlib import Path

# Chemins des templates
TEMPLATES_DIR = Path("templates/legal")
ARCHIVES_DIR = TEMPLATES_DIR / "archives"

# Fichiers des politiques
POLICY_FILES = {
    'cgu': 'terms.html',
    'confidentialite': 'privacy.html',
    'cookies': 'cookies.html'
}


def archive_policy(policy_type, version_date=None):
    """
    Archive une politique légale avant modification

    Args:
        policy_type (str): Type de politique ('cgu', 'confidentialite', 'cookies')
        version_date (str): Date de version au format YYYY-MM-DD (optionnel, utilise date actuelle par défaut)

    Returns:
        str: Chemin du fichier archivé
    """
    if policy_type not in POLICY_FILES:
        raise ValueError(f"Type de politique invalide: {policy_type}")

    # Créer le dossier d'archives si nécessaire
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)

    # Fichier source
    source_file = TEMPLATES_DIR / POLICY_FILES[policy_type]

    if not source_file.exists():
        raise FileNotFoundError(f"Fichier de politique introuvable: {source_file}")

    # Date de version
    if not version_date:
        version_date = datetime.now().strftime('%Y-%m-%d')

    # Nom du fichier archivé (exemple: terms_2025-10-13.html)
    policy_name = POLICY_FILES[policy_type].replace('.html', '')
    archive_filename = f"{policy_name}_{version_date}.html"
    archive_path = ARCHIVES_DIR / archive_filename

    # Copier le fichier
    shutil.copy2(source_file, archive_path)

    print(f"✅ Politique archivée: {archive_path}")
    return str(archive_path)


def list_archives(policy_type=None):
    """
    Liste toutes les archives disponibles

    Args:
        policy_type (str): Type de politique à filtrer (optionnel)

    Returns:
        list: Liste des fichiers archivés
    """
    if not ARCHIVES_DIR.exists():
        return []

    archives = []
    for file in ARCHIVES_DIR.glob('*.html'):
        if policy_type:
            policy_name = POLICY_FILES.get(policy_type, '').replace('.html', '')
            if file.name.startswith(policy_name):
                archives.append(file)
        else:
            archives.append(file)

    return sorted(archives, reverse=True)  # Plus récent en premier


def get_archive_info(archive_file):
    """
    Extrait les informations d'un fichier archivé

    Args:
        archive_file (Path): Fichier archivé

    Returns:
        dict: Informations de l'archive
    """
    filename = archive_file.name
    parts = filename.replace('.html', '').split('_')

    if len(parts) >= 2:
        policy_name = parts[0]
        version_date = '_'.join(parts[1:])  # Gérer les dates avec underscore

        # Déterminer le type de politique
        policy_type = None
        for key, val in POLICY_FILES.items():
            if val.startswith(policy_name):
                policy_type = key
                break

        return {
            'filename': filename,
            'path': str(archive_file),
            'policy_type': policy_type,
            'policy_name': policy_name,
            'version_date': version_date,
            'size': archive_file.stat().st_size,
            'modified': datetime.fromtimestamp(archive_file.stat().st_mtime)
        }

    return None


def restore_archive(archive_file, confirm=False):
    """
    Restaure une version archivée d'une politique

    Args:
        archive_file (str|Path): Fichier archivé à restaurer
        confirm (bool): Confirmation de restauration (sécurité)

    Returns:
        bool: True si restauration réussie
    """
    if not confirm:
        raise ValueError("La restauration nécessite une confirmation explicite (confirm=True)")

    archive_path = Path(archive_file)
    if not archive_path.exists():
        raise FileNotFoundError(f"Archive introuvable: {archive_file}")

    # Déterminer le fichier de destination
    info = get_archive_info(archive_path)
    if not info or not info['policy_type']:
        raise ValueError("Impossible de déterminer le type de politique de cette archive")

    dest_file = TEMPLATES_DIR / POLICY_FILES[info['policy_type']]

    # SÉCURITÉ: Archiver la version actuelle avant restauration
    current_version_date = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    archive_policy(info['policy_type'], current_version_date)

    # Restaurer l'archive
    shutil.copy2(archive_path, dest_file)

    print(f"✅ Politique restaurée depuis: {archive_path}")
    print(f"✅ Version actuelle archivée avec timestamp: {current_version_date}")
    return True


if __name__ == "__main__":
    # Exemple d'utilisation
    print("=== Système d'archivage des politiques légales ===\n")

    # Archiver toutes les politiques actuelles
    print("1. Archivage des politiques actuelles...")
    for policy_type in POLICY_FILES.keys():
        try:
            archive_policy(policy_type, "2025-10-13")
        except Exception as e:
            print(f"❌ Erreur pour {policy_type}: {e}")

    print("\n2. Liste des archives:")
    all_archives = list_archives()
    for archive in all_archives:
        info = get_archive_info(archive)
        if info:
            print(f"  - {info['filename']} ({info['policy_type']}) - {info['version_date']}")
