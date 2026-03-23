#!/usr/bin/env python3
"""
Script pour réinitialiser un mot de passe utilisateur
"""
import os
import sys

# Ajouter le répertoire courant au path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, db
from models import User
from werkzeug.security import generate_password_hash

def reset_password(email, new_password):
    try:
        # app is already imported directly
        with app.app_context():
            user = User.query.filter_by(email=email).first()
            if user:
                user.password_hash = generate_password_hash(new_password)
                user.must_change_password = False  # Permettre la connexion directe
                db.session.commit()
                print(f"Mot de passe réinitialisé pour {email}")
                return True
            else:
                print(f"Utilisateur {email} non trouvé")
                return False
    except Exception as e:
        print(f"Erreur: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python reset_password.py <email> <new_password>")
        sys.exit(1)

    email = sys.argv[1]
    new_password = sys.argv[2]
    success = reset_password(email, new_password)

    if success:
        print("\nMot de passe réinitialisé avec succès !")
    else:
        print("\nErreur lors de la réinitialisation du mot de passe.")
        sys.exit(1)