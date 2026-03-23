#!/usr/bin/env python3
"""
Script de démarrage personnalisé pour FinovRelance
Démarre Gunicorn avec des paramètres optimisés pour les grandes signatures
"""

import subprocess
import sys
import os

def start_server():
    """Démarre le serveur Gunicorn avec configuration optimisée"""

    enable_reload = os.environ.get('GUNICORN_RELOAD', 'false').lower() == 'true'
    reload_status = "activé" if enable_reload else "désactivé"

    print("Démarrage de FinovRelance avec configuration optimisée...")
    print(f"Support signatures jusqu'a 2.5MB")
    print(f"Reload: {reload_status}")
    print("-" * 60)

    try:
        cmd = [
            'gunicorn',
            '--bind', '0.0.0.0:5000',
            '--reuse-port',
            '--workers', '1',
            '--timeout', '120',
            '--max-requests', '1000',
            '--max-requests-jitter', '100',
            '--limit-request-line', '8192',
            '--limit-request-field_size', '32768',
            '--limit-request-fields', '200',
            '--access-logfile', '-',
            '--error-logfile', '-',
            'main:app'
        ]

        if enable_reload:
            cmd.insert(4, '--reload')

        subprocess.run(cmd, check=True, shell=False)

    except subprocess.CalledProcessError as e:
        print(f"Erreur lors du demarrage: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nArret du serveur...")
        sys.exit(0)

if __name__ == '__main__':
    start_server()