#!/usr/bin/env python3
"""
Script d'audit de sécurité multi-tenant
Détecte les failles potentielles d'isolation entre entreprises
"""

import os
import re
from collections import defaultdict

# Modèles avec company_id qui doivent être protégés
MODELS_WITH_COMPANY_ID = [
    'Client', 'Invoice', 'Note', 'Campaign', 'CampaignTemplate',
    'EmailLog', 'Reminder', 'AccountingConnection', 'BusinessCentralConfig',
    'SyncLog', 'Notification', 'UserCompany', 'CompanySyncUsage'
]

# Patterns dangereux
DANGEROUS_PATTERNS = [
    (r'\.query\.get\(', 'Usage de .query.get() - vulnérable si pas de vérification company_id après'),
    (r'\.query\.filter_by\([^)]*\)\.first\(\)', 'filter_by sans company_id - vérifier si company_id dans les filtres'),
    (r'session\.get\(["\']selected_company_id["\']\)', 'Accès à selected_company_id - vérifier utilisation sécurisée'),
]

# Patterns sécurisés (whitelist)
SAFE_PATTERNS = [
    r'safe_get_by_id\(',
    r'current_user\.get_selected_company\(\)',
    r'\.filter_by\([^)]*company_id[^)]*\)',
    r'# SÉCURITÉ:',
    r'# SECURITE:',
]

def is_safe_context(line, context_before, context_after):
    """Vérifie si le code dangereux est dans un contexte sécurisé"""

    # Vérifier les 3 lignes avant et après pour patterns sécurisés
    full_context = '\n'.join(context_before + [line] + context_after)

    for safe_pattern in SAFE_PATTERNS:
        if re.search(safe_pattern, full_context):
            return True

    # Vérifier si c'est dans une fonction safe_get_by_id (définition)
    if 'def safe_get_by_id' in full_context:
        return True

    return False

def audit_file(filepath):
    """Audite un fichier Python pour les failles de sécurité"""
    findings = []

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        for i, line in enumerate(lines):
            for pattern, description in DANGEROUS_PATTERNS:
                if re.search(pattern, line):
                    # Récupérer le contexte (3 lignes avant et après)
                    context_before = lines[max(0, i-3):i]
                    context_after = lines[i+1:min(len(lines), i+4)]

                    # Vérifier si c'est dans un contexte sécurisé
                    if not is_safe_context(line, context_before, context_after):
                        findings.append({
                            'file': filepath,
                            'line': i + 1,
                            'code': line.strip(),
                            'issue': description,
                            'severity': 'HIGH' if '.query.get(' in line else 'MEDIUM'
                        })

    except Exception as e:
        print(f"Erreur lecture {filepath}: {e}")

    return findings

def audit_codebase():
    """Audite tout le codebase"""
    print("🔍 AUDIT DE SÉCURITÉ MULTI-TENANT")
    print("=" * 80)

    findings_by_file = defaultdict(list)
    total_findings = 0

    # Fichiers Python à auditer
    python_files = [
        'views.py', 'views/auth_views.py', 'views/client_views.py',
        'views/company_views.py', 'views/email_views.py', 'views/user_views.py',
        'notification_routes.py', 'stripe_checkout_v2.py',
        'business_central_connector.py', 'quickbooks_connector.py',
        'utils.py', 'models.py'
    ]

    # Auditer chaque fichier
    for file_path in python_files:
        if os.path.exists(file_path):
            findings = audit_file(file_path)
            if findings:
                findings_by_file[file_path] = findings
                total_findings += len(findings)

    # Afficher les résultats
    if total_findings == 0:
        print("✅ AUCUNE FAILLE DÉTECTÉE - Le code semble sécurisé!")
        return

    print(f"⚠️  {total_findings} FAILLES POTENTIELLES DÉTECTÉES\n")

    for file_path, findings in sorted(findings_by_file.items()):
        print(f"\n📁 {file_path} ({len(findings)} failles)")
        print("-" * 80)

        for finding in findings:
            severity_icon = "🔴" if finding['severity'] == 'HIGH' else "🟡"
            print(f"{severity_icon} Ligne {finding['line']}: {finding['issue']}")
            print(f"   Code: {finding['code']}")

    print("\n" + "=" * 80)
    print("\n📋 RÉSUMÉ:")
    print(f"   - Total de failles: {total_findings}")
    print(f"   - Fichiers affectés: {len(findings_by_file)}")

    # Recommandations
    print("\n💡 RECOMMANDATIONS:")
    print("   1. Remplacer .query.get() par safe_get_by_id() avec company_id")
    print("   2. Ajouter company_id dans tous les .filter_by() sur modèles multi-tenant")
    print("   3. Utiliser current_user.get_selected_company() au lieu de session['selected_company_id']")
    print("   4. Vérifier ownership avec require_company_ownership() après récupération")
    print("   5. Marquer le code vérifié avec commentaire # SÉCURITÉ: ...")

def main():
    audit_codebase()

if __name__ == "__main__":
    main()
