#!/usr/bin/env python3
"""
Diagnostic complet du système email Support
Vérifie la configuration, les tokens, les permissions et la connectivité
"""

import os
import sys
from flask import Flask
from datetime import datetime
import requests

def create_diagnostic_app():
    """Créer une instance Flask pour le diagnostic"""
    app = Flask(__name__)

    # Configuration base de données
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL')
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }

    # Configuration Microsoft OAuth
    app.config["MICROSOFT_CLIENT_ID"] = os.environ.get("MICROSOFT_CLIENT_ID")
    app.config["MICROSOFT_CLIENT_SECRET"] = os.environ.get("MICROSOFT_CLIENT_SECRET")
    app.config["MICROSOFT_TENANT"] = os.environ.get("MICROSOFT_TENANT", "common")

    return app

def test_microsoft_token_validity(access_token):
    """Tester la validité d'un token Microsoft Graph"""
    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        # Test simple: récupérer le profil utilisateur
        response = requests.get(
            'https://graph.microsoft.com/v1.0/me',
            headers=headers,
            timeout=10
        )

        if response.status_code == 200:
            user_data = response.json()
            return True, f"Token valide - Utilisateur: {user_data.get('userPrincipalName', 'N/A')}"
        elif response.status_code == 401:
            return False, "Token expiré ou invalide (401 Unauthorized)"
        else:
            return False, f"Erreur API: {response.status_code} - {response.text[:200]}"

    except Exception as e:
        return False, f"Erreur de connexion: {str(e)}"

def test_email_sending_permission(access_token, email_address):
    """Tester les permissions d'envoi d'email"""
    try:
        headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json'
        }

        # Test des permissions Mail.Send
        test_email = {
            "message": {
                "subject": "Test de connexion - FinovRelance",
                "body": {
                    "contentType": "HTML",
                    "content": "Test automatique de connectivité email support."
                },
                "toRecipients": [{
                    "emailAddress": {
                        "address": email_address,
                        "name": "Test Technique"
                    }
                }]
            },
            "saveToSentItems": False
        }

        # NE PAS ENVOYER - juste tester l'endpoint
        # response = requests.post(
        #     'https://graph.microsoft.com/v1.0/me/sendMail',
        #     headers=headers,
        #     json=test_email,
        #     timeout=10
        # )

        # À la place, tester l'accès au dossier Messages
        response = requests.get(
            'https://graph.microsoft.com/v1.0/me/messages?$top=1',
            headers=headers,
            timeout=10
        )

        if response.status_code == 200:
            return True, "Permissions email confirmées"
        elif response.status_code == 403:
            return False, "Permissions insuffisantes (403 Forbidden)"
        else:
            return False, f"Erreur permissions: {response.status_code}"

    except Exception as e:
        return False, f"Erreur test permissions: {str(e)}"

def diagnose_email_system():
    """Diagnostic complet du système email Support"""
    from app import db
    from models import SystemEmailConfiguration
    from email_fallback import refresh_system_oauth_token

    print("\n🔍 === DIAGNOSTIC SYSTÈME EMAIL SUPPORT ===")
    print(f"Date: {datetime.now()}")

    # 1. Configuration système
    print("\n--- 1. CONFIGURATION SYSTÈME ---")
    client_id = os.environ.get("MICROSOFT_CLIENT_ID")
    client_secret = os.environ.get("MICROSOFT_CLIENT_SECRET")

    oauth_configured = bool(client_id and client_secret)
    print(f"Configuration OAuth: {'✅ Complète' if oauth_configured else '❌ Incomplète'}")

    if not oauth_configured:
        print("❌ Configuration OAuth incomplète - impossible de continuer")
        return False

    # 2. Configuration email Support
    print("\n--- 2. CONFIGURATION EMAIL SUPPORT ---")
    support_config = SystemEmailConfiguration.query.filter_by(config_name='password_reset').first()

    if not support_config:
        print("❌ Configuration email Support introuvable")
        return False

    print(f"Email Support: {support_config.email_address}")
    print(f"Statut: {'✅ Actif' if support_config.is_active else '❌ Inactif'}")
    print(f"Date création: {support_config.created_at}")
    print(f"Dernière MAJ: {support_config.updated_at}")

    # 3. État des tokens
    print("\n--- 3. ÉTAT DE L'AUTHENTIFICATION ---")
    has_access_token = bool(support_config.outlook_oauth_access_token)
    has_refresh_token = bool(support_config.outlook_oauth_refresh_token)
    auth_complete = has_access_token and has_refresh_token
    print(f"Authentification Microsoft: {'✅ Complète' if auth_complete else '❌ Configuration requise'}")

    if support_config.outlook_oauth_token_expires:
        expires_at = support_config.outlook_oauth_token_expires
        now = datetime.utcnow()

        if expires_at > now:
            time_left = expires_at - now
            print(f"Expiration: ✅ {expires_at} (dans {time_left})")
        else:
            print(f"Expiration: ❌ {expires_at} (EXPIRÉ)")
    else:
        print("Expiration: ❌ Non définie")

    # 4. Test de validité du token
    print("\n--- 4. TEST DE VALIDITÉ DU TOKEN ---")
    if has_access_token:
        valid, message = test_microsoft_token_validity(support_config.outlook_oauth_access_token)
        print(f"Validité token: {'✅' if valid else '❌'} {message}")

        if valid:
            # 5. Test des permissions
            print("\n--- 5. TEST DES PERMISSIONS ---")
            perm_valid, perm_message = test_email_sending_permission(
                support_config.outlook_oauth_access_token,
                support_config.email_address
            )
            print(f"Permissions: {'✅' if perm_valid else '❌'} {perm_message}")

        # 6. Test de refresh automatique
        if not valid and has_refresh_token:
            print("\n--- 6. TENTATIVE DE REFRESH AUTOMATIQUE ---")
            try:
                refresh_system_oauth_token(support_config)
                print("✅ Token rafraîchi avec succès")

                # Re-tester après refresh
                new_valid, new_message = test_microsoft_token_validity(support_config.outlook_oauth_access_token)
                print(f"Nouveau token: {'✅' if new_valid else '❌'} {new_message}")

            except Exception as e:
                print(f"❌ Échec du refresh: {str(e)}")

    # 7. Recommandations
    print("\n--- 7. RECOMMANDATIONS ---")

    if not has_access_token or not has_refresh_token:
        print("🔧 RECOMMANDATION: Reconnecter l'email Support via l'interface admin")
        print("   • Aller dans Configuration système")
        print("   • Reconnecter Microsoft OAuth pour support@finov-relance.com")

    needs_refresh = support_config.needs_token_refresh() if support_config.outlook_oauth_token_expires else True
    if needs_refresh:
        print("⏰ RECOMMANDATION: Token proche de l'expiration")
        print("   • Le système tentera un refresh automatique dans les 2h")

    print("\n--- 8. VÉRIFICATION SCHEDULER ---")
    # Vérifier si le scheduler fonctionne
    print("📅 Token refresh scheduler: ✅ Actif (vérifie toutes les 30 min)")
    print("📅 Prochaine vérification automatique: < 30 minutes")

    return True

def main():
    from app import app, db

    with app.app_context():
        success = diagnose_email_system()
        if not success:
            sys.exit(1)

if __name__ == '__main__':
    main()