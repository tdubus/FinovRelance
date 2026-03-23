"""
QuickBooks Online Connector
Handles OAuth authentication and data synchronization with QuickBooks Online API

IMPORTANT: Updated for QuickBooks API Minor Version 75
- As of August 1, 2025, QuickBooks Online requires minorversion=75 for all API requests
- Previous minor versions 1-74 are deprecated and will be ignored
- All API requests automatically include minorversion=75 parameter
- Code is designed to handle future schema additions gracefully by ignoring unknown fields

⚠️ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔒 ATTENTION - CODE EN PRODUCTION COMMERCIALE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

❌ INTERDICTION ABSOLUE DE MODIFIER CE FICHIER SANS AUTORISATION EXPLICITE

FinovRelance est un produit commercialisé avec des clients payants.
Ce connecteur QuickBooks gère des données financières critiques et des transactions
sensibles pour des entreprises réelles.

📋 RÈGLES STRICTES:
- AUCUNE modification de code sans accord préalable du propriétaire
- AUCUNE modification des routes, templates ou logique OAuth
- Toute demande doit être documentée et approuvée formellement
- En cas de bug: proposer diagnostic SANS toucher au code

🚨 CONSÉQUENCES D'UNE MODIFICATION NON AUTORISÉE:
- Interruption de service pour clients payants
- Perte de données financières
- Violation de conformité fiscale
- Compromission de sécurité (isolation entreprises)
- Responsabilité légale et financière

✅ ACTIONS AUTORISÉES: Lecture, diagnostic, analyse, documentation uniquement
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import logging
import requests
import base64
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode
from flask import current_app, url_for, session

# Imports locaux pour éviter les imports circulaires
# Ces imports sont fait dans les fonctions qui en ont besoin


class QuickBooksConnector:
    """QuickBooks Online API connector"""

    # QuickBooks OAuth 2.0 endpoints
    AUTHORIZATION_URL = 'https://appcenter.intuit.com/connect/oauth2'
    TOKEN_URL = 'https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer'

    # Determine if using sandbox or production based on environment
    def get_discovery_url(self):
        """Get the appropriate QuickBooks API URL based on environment"""
        if current_app.config.get('QUICKBOOKS_SANDBOX', False):
            return 'https://sandbox-quickbooks.api.intuit.com'
        else:
            return 'https://quickbooks.api.intuit.com'

    # Required scopes for QuickBooks Online (as per documentation)
    SCOPES = 'com.intuit.quickbooks.accounting'

    def __init__(self, connection_id: Optional[int] = None, company_id: Optional[int] = None):
        """Initialize connector with optional existing connection

        Args:
            connection_id: ID of the QuickBooks connection
            company_id: ID of the company that owns the connection (for security validation)
        """
        import os
        from flask import abort

        self.connection = None
        if connection_id:
            from models import AccountingConnection
            self.connection = AccountingConnection.query.get(connection_id)

            # SÉCURITÉ CRITIQUE: Vérifier que la connexion appartient à l'entreprise spécifiée
            if self.connection and company_id and self.connection.company_id != company_id:
                # Protection contre IDOR - une entreprise ne peut pas utiliser les connexions d'une autre
                raise ValueError(f"SÉCURITÉ: Tentative d'accès à une connexion QuickBooks non autorisée. Connection {connection_id} n'appartient pas à l'entreprise {company_id}")

            # Vérification supplémentaire si pas de company_id fourni
            if self.connection and not company_id:
                logging.warning(f"SÉCURITÉ: Connexion QuickBooks {connection_id} chargée sans validation de l'entreprise")

        # Load credentials from environment variables
        self.client_id = os.environ.get('QUICKBOOKS_CLIENT_ID')
        self.client_secret = os.environ.get('QUICKBOOKS_CLIENT_SECRET')
        self.is_sandbox = current_app.config.get('QUICKBOOKS_SANDBOX', False)

    def get_authorization_url(self, state: str) -> str:
        """Generate OAuth authorization URL for QuickBooks according to official documentation"""
        if not self.client_id:
            raise ValueError("QuickBooks Client ID not configured")

        from flask import url_for

        redirect_uri = url_for('company.quickbooks_callback', _external=True)

        # Build authorization URL manually as per QuickBooks documentation
        # https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0#authorization-request
        params = {
            'client_id': self.client_id,
            'scope': self.SCOPES,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'access_type': 'offline',
            'state': state
        }

        authorization_url = f"{self.AUTHORIZATION_URL}?{urlencode(params)}"
        return authorization_url

    def exchange_code_for_tokens(self, authorization_code: str, realm_id: str, state: str) -> Dict:
        """Exchange authorization code for access and refresh tokens according to QuickBooks documentation"""
        if not self.client_id or not self.client_secret:
            raise ValueError("QuickBooks credentials not configured")

        from flask import url_for

        redirect_uri = url_for('company.quickbooks_callback', _external=True)

        # Prepare token request as per QuickBooks documentation
        # https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0#exchange-the-code-for-tokens

        # Base64 encode client credentials
        credentials = f"{self.client_id}:{self.client_secret}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        headers = {
            'Authorization': f'Basic {encoded_credentials}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        data = {
            'grant_type': 'authorization_code',
            'code': authorization_code,
            'redirect_uri': redirect_uri
        }

        # SÉCURITÉ ÉTAPE 8 : Utiliser session HTTP robuste pour appels externes
        from utils.http_client import create_quickbooks_session

        session = create_quickbooks_session()
        response = session.post(self.TOKEN_URL, headers=headers, data=data)

        token_data = response.json()

        # Calculate token expiration
        expires_at = datetime.utcnow() + timedelta(seconds=token_data.get('expires_in', 3600))

        return {
            'access_token': token_data['access_token'],
            'refresh_token': token_data.get('refresh_token'),
            'expires_at': expires_at,
            'realm_id': realm_id
        }

    def refresh_access_token(self) -> bool:
        """Refresh the access token using refresh token"""
        if not self.connection or not self.connection.refresh_token:
            return False

        if not self.client_id or not self.client_secret:
            raise ValueError("QuickBooks credentials not configured")

        # Base64 encode client credentials
        credentials = f"{self.client_id}:{self.client_secret}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        headers = {
            'Authorization': f'Basic {encoded_credentials}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        data = {
            'grant_type': 'refresh_token',
            'refresh_token': self.connection.refresh_token
        }

        try:
            response = requests.post(self.TOKEN_URL, headers=headers, data=data)
            response.raise_for_status()

            token_data = response.json()

            # Update connection with new tokens
            self.connection.access_token = token_data['access_token']
            if 'refresh_token' in token_data:
                self.connection.refresh_token = token_data['refresh_token']

            self.connection.token_expires_at = datetime.utcnow() + timedelta(
                seconds=token_data.get('expires_in', 3600)
            )

            from app import db
            db.session.commit()
            return True

        except Exception as e:
            logging.error(f"Failed to refresh QuickBooks token: {e}")
            return False

    def make_api_request(self, endpoint: str, method: str = 'GET', params: Dict = None) -> Dict:
        """Make authenticated API request to QuickBooks"""
        if not self.connection:
            raise ValueError("No QuickBooks connection available")

        # Check if token needs refreshing
        if not self.connection.is_token_valid():
            if not self.refresh_access_token():
                raise ValueError("Failed to refresh QuickBooks access token")

        base_url = f"{self.get_discovery_url()}/v3/company/{self.connection.company_id_external}"
        url = f"{base_url}/{endpoint}"

        headers = {
            'Authorization': f'Bearer {self.connection.access_token}',
            'Accept': 'application/json'
        }

        # Always include minorversion=75 as required by QuickBooks API changes (August 1, 2025)
        if params is None:
            params = {}
        params['minorversion'] = '75'

        # SÉCURITÉ ÉTAPE 8 : Utiliser session HTTP robuste pour appels externes
        from utils.http_client import create_quickbooks_session

        session = create_quickbooks_session()
        response = session.request(method, url, headers=headers, params=params)

        return response.json()

    def test_connection(self) -> Tuple[bool, str]:
        """Test the QuickBooks connection"""
        try:
            # Try to fetch company info
            response = self.make_api_request('companyinfo/1')
            company_info = response.get('QueryResponse', {}).get('CompanyInfo', [])

            if company_info:
                return True, f"Connexion réussie à {company_info[0].get('CompanyName', 'QuickBooks')}"
            else:
                return False, "Impossible de récupérer les informations de l'entreprise"

        except Exception as e:
            return False, f"Erreur de connexion: {str(e)}"

    def sync_customers(self, company_id: int, sync_log_id: Optional[int] = None) -> Tuple[int, int]:
        """Sync customers from QuickBooks to local database

        Args:
            company_id: ID of the company to sync data for
            sync_log_id: Optional ID of the SyncLog entry for tracking and manual stop
        """
        created_count = 0
        updated_count = 0
        manual_stop_requested = False
        sync_log = None

        try:
            from app import db
            from models import Client, CompanySyncUsage, SyncLog

            # Charger le SyncLog si fourni pour supporter l'arrêt manuel
            if sync_log_id:
                sync_log = SyncLog.query.get(sync_log_id)

            # SÉCURITÉ CRITIQUE: Vérifier l'intégrité des données
            if not self.connection:
                raise ValueError("Aucune connexion QuickBooks configurée")

            if self.connection.company_id != company_id:
                raise ValueError(f"SÉCURITÉ: Tentative de synchronisation croisée détectée. Connexion appartient à l'entreprise {self.connection.company_id} mais tentative de sync pour l'entreprise {company_id}")

            # Vérifier les limites de synchronisation avant de commencer
            if not CompanySyncUsage.check_company_sync_limit(company_id):
                raise Exception("Limite quotidienne de synchronisation atteinte pour votre forfait.")

            # Vérifier que les clés QuickBooks sont configurées
            if not self.client_id or not self.client_secret:
                raise Exception("QuickBooks credentials not configured")

            # Fetch ALL customers from QuickBooks with pagination
            all_customers = []
            start_position = 1
            max_results = 1000  # Maximum autorisé par QuickBooks

            while True:
                # Vérifier si un arrêt manuel a été demandé
                if sync_log:
                    db.session.refresh(sync_log)
                    if sync_log.is_stop_requested():
                        manual_stop_requested = True
                        logging.warning(f"🛑 Arrêt manuel demandé pour QuickBooks sync_customers (SyncLog {sync_log_id})")
                        break

                query = f"SELECT * FROM Customer STARTPOSITION {start_position} MAXRESULTS {max_results}"
                response = self.make_api_request('query', params={'query': query})
                query_response = response.get('QueryResponse', {})

                if not query_response:
                    break

                customers_batch = query_response.get('Customer', [])
                if not customers_batch:
                    break

                all_customers.extend(customers_batch)

                # Vérifier s'il y a plus de résultats
                if len(customers_batch) < max_results:
                    break

                start_position += max_results

            customers = all_customers
            print(f"Total clients récupérés de QuickBooks: {len(customers)}")

            field_mapping = self.connection.get_field_mapping()

            # SÉCURITÉ LICENCE: Compter les nouveaux clients avant création
            from models import Company
            company = Company.query.get(company_id)
            if not company:
                raise ValueError(f"Entreprise {company_id} non trouvée")

            new_clients_count = 0
            for qb_customer in customers:
                client_data = self._map_customer_fields(qb_customer, field_mapping)
                # Vérifier si le client existe déjà
                existing_client = Client.query.filter_by(
                    code_client=client_data['code_client'],
                    company_id=company_id
                ).first()
                if not existing_client:
                    new_clients_count += 1

            # VÉRIFICATION CRUCIALE: Respect des limites de plan
            if new_clients_count > 0:
                try:
                    company.assert_client_capacity(new_clients_count)
                    print(f"✅ QuickBooks sync autorisé: {new_clients_count} nouveaux clients (plan: {company.get_plan_display_name()})")
                except ValueError as e:
                    raise Exception(f"🚫 Import QuickBooks bloqué: {str(e)}")

            # Garder la trace des clients QuickBooks synchronisés
            qb_client_codes = []
            parent_relationships = []  # Pour traiter les relations parent/enfant en seconde phase

            # Phase 1: Créer/mettre à jour tous les clients
            for qb_customer in customers:
                # Map QuickBooks fields to local fields
                client_data = self._map_customer_fields(qb_customer, field_mapping)
                client_data['company_id'] = company_id

                # Ajouter le code client à la liste des clients QB synchronisés
                qb_client_codes.append(client_data['code_client'])

                # Stocker les relations parent/enfant pour traitement ultérieur
                if '_parent_name' in client_data:
                    parent_relationships.append((client_data['code_client'], client_data['_parent_name']))
                    del client_data['_parent_name']  # Supprimer le champ temporaire
                elif '_parent_id' in client_data:
                    parent_relationships.append((client_data['code_client'], client_data['_parent_id']))
                    del client_data['_parent_id']  # Supprimer le champ temporaire

                # Check if client already exists
                existing_client = Client.query.filter_by(
                    code_client=client_data['code_client'],
                    company_id=company_id
                ).first()

                if existing_client:
                    # Update existing client (préserver le collecteur assigné)
                    for key, value in client_data.items():
                        if key not in ['id', 'collector_id', 'is_parent']:  # Protéger le collecteur et les propriétés calculées
                            setattr(existing_client, key, value)
                    updated_count += 1
                    print(f"Client mis à jour: {client_data['code_client']}")
                else:
                    # Create new client (sans collecteur assigné)
                    client_data['collector_id'] = None  # Pas de collecteur à l'import QB
                    # Remove calculated properties before creating the client
                    filtered_data = {k: v for k, v in client_data.items() if k not in ['is_parent']}
                    new_client = Client(**filtered_data)
                    db.session.add(new_client)
                    created_count += 1
                    print(f"Nouveau client créé: {client_data['code_client']}")

            # Commit phase 1 pour avoir les IDs des clients créés
            db.session.commit()

            # Phase 2: Traiter les relations parent/enfant
            if parent_relationships:
                all_clients = {client.code_client: client for client in Client.query.filter_by(company_id=company_id).all()}
                # Créer aussi un mapping par ID QuickBooks pour les parents
                qb_to_client = {}
                for client in all_clients.values():
                    # Extraire l'ID QuickBooks du code_client (format: nom ou QB_ID)
                    if client.code_client.startswith('QB_'):
                        qb_id = client.code_client[3:]  # Enlever 'QB_' prefix
                        qb_to_client[qb_id] = client
                    else:
                        # Pour les clients avec nom comme code_client, chercher dans les données QB
                        for qb_customer in customers:
                            if qb_customer.get('DisplayName') == client.code_client:
                                qb_to_client[qb_customer.get('Id')] = client
                                break

                for child_code, parent_identifier in parent_relationships:
                    if child_code in all_clients:
                        child_client = all_clients[child_code]
                        parent_client = None

                        # Chercher le parent par nom d'abord
                        if parent_identifier in all_clients:
                            parent_client = all_clients[parent_identifier]
                        # Sinon par ID QuickBooks
                        elif parent_identifier in qb_to_client:
                            parent_client = qb_to_client[parent_identifier]

                        if parent_client:
                            child_client.parent_client_id = parent_client.id
                        else:
                            logging.warning(f"Relation parent/enfant ignorée: {child_code} -> {parent_identifier} (parent non trouvé)")

                # Commit final pour les relations parent/enfant
                db.session.commit()

            # CRITIQUE: Re-vérifier l'arrêt manuel après le traitement pour éviter la race condition
            if sync_log and not manual_stop_requested:
                db.session.refresh(sync_log)
                if sync_log.is_stop_requested():
                    manual_stop_requested = True
                    logging.warning(f"🛑 Arrêt manuel détecté en finalisation pour QuickBooks sync_customers {sync_log_id}")

            # Mettre à jour le SyncLog final
            if sync_log:
                if manual_stop_requested:
                    sync_log.acknowledge_stop()
                    logging.warning(f"🛑 QuickBooks sync_customers arrêtée manuellement après {created_count} créations, {updated_count} mises à jour")
                else:
                    sync_log.status = 'completed'
                    sync_log.completed_at = datetime.utcnow()
                    sync_log.clients_synced = created_count + updated_count
                db.session.commit()

            # Incrémenter le compteur de synchronisation après succès (sauf si arrêt manuel)
            if not manual_stop_requested:
                CompanySyncUsage.increment_company_sync_count(company_id)

        except Exception as e:
            logging.error(f"Error syncing customers from QuickBooks: {e}")
            if sync_log:
                sync_log.status = 'failed'
                sync_log.error_message = str(e)
                sync_log.completed_at = datetime.utcnow()
                db.session.commit()
            db.session.rollback()
            raise

        return created_count, updated_count

    def sync_invoices(self, company_id: int, sync_log_id: Optional[int] = None) -> Tuple[int, int]:
        """Sync invoices from QuickBooks to local database

        Args:
            company_id: ID of the company to sync data for
            sync_log_id: Optional ID of the SyncLog entry for tracking and manual stop
        """
        print("=== STARTING INVOICE SYNC ===")
        print(f"Starting invoice sync for company {company_id}")
        created_count = 0
        updated_count = 0
        manual_stop_requested = False
        sync_log = None

        try:
            from app import db
            from models import Client, Invoice, CompanySyncUsage, SyncLog

            # Charger le SyncLog si fourni pour supporter l'arrêt manuel
            if sync_log_id:
                sync_log = SyncLog.query.get(sync_log_id)

            # SÉCURITÉ CRITIQUE: Vérifier l'intégrité des données
            if not self.connection:
                raise ValueError("Aucune connexion QuickBooks configurée")

            if self.connection.company_id != company_id:
                raise ValueError(f"SÉCURITÉ: Tentative de synchronisation croisée détectée. Connexion appartient à l'entreprise {self.connection.company_id} mais tentative de sync pour l'entreprise {company_id}")

            # Vérifier les limites de synchronisation avant de commencer
            if not CompanySyncUsage.check_company_sync_limit(company_id):
                raise Exception("Limite quotidienne de synchronisation atteinte pour votre forfait.")

            # Vérifier que les clés QuickBooks sont configurées
            if not self.client_id or not self.client_secret:
                raise Exception("QuickBooks credentials not configured")

            # Fetch ALL invoices from QuickBooks with pagination
            all_invoices = []
            start_position = 1
            max_results = 1000  # Maximum autorisé par QuickBooks

            while True:
                # Vérifier si un arrêt manuel a été demandé
                if sync_log:
                    db.session.refresh(sync_log)
                    if sync_log.is_stop_requested():
                        manual_stop_requested = True
                        logging.warning(f"🛑 Arrêt manuel demandé pour QuickBooks sync_invoices (SyncLog {sync_log_id})")
                        break

                query = f"SELECT * FROM Invoice WHERE Balance > '0.0' STARTPOSITION {start_position} MAXRESULTS {max_results}"
                response = self.make_api_request('query', params={'query': query})
                print(f"Batch QuickBooks response for invoices {start_position}-{start_position + max_results - 1}")

                query_response = response.get('QueryResponse', {})

                if not query_response:
                    break

                invoices_batch = query_response.get('Invoice', [])
                if not invoices_batch:
                    break

                all_invoices.extend(invoices_batch)

                # Vérifier s'il y a plus de résultats
                if len(invoices_batch) < max_results:
                    break

                start_position += max_results

            invoices = all_invoices
            print(f"Total factures récupérées de QuickBooks: {len(invoices)}")

            field_mapping = self.connection.get_field_mapping()

            for qb_invoice in invoices:
                # Check if invoice has unpaid balance
                balance = float(qb_invoice.get('Balance', 0))
                print(f"Invoice {qb_invoice.get('DocNumber', 'N/A')} - Balance: {balance}")

                # Delete fully paid invoices (Balance = 0) from local database
                if balance <= 0:
                    print(f"Removing paid invoice {qb_invoice.get('DocNumber', 'N/A')} from local database")
                    # Find and delete the paid invoice if it exists locally
                    from models import Invoice
                    existing_paid_invoice = Invoice.query.filter_by(
                        invoice_number=qb_invoice.get('DocNumber'),
                        company_id=company_id
                    ).first()
                    if existing_paid_invoice:
                        db.session.delete(existing_paid_invoice)
                        print(f"Deleted paid invoice {existing_paid_invoice.invoice_number} from local database")
                    continue

                print(f"Processing unpaid invoice {qb_invoice.get('DocNumber', 'N/A')} (Balance: {balance})")
                # Map QuickBooks fields to local fields
                invoice_data = self._map_invoice_fields(qb_invoice, field_mapping, company_id)

                if not invoice_data:  # Skip if client not found
                    continue

                # Check if invoice already exists
                existing_invoice = Invoice.query.filter_by(
                    invoice_number=invoice_data['invoice_number'],
                    client_id=invoice_data['client_id']
                ).first()

                if existing_invoice:
                    # Update existing invoice
                    for key, value in invoice_data.items():
                        if key != 'id':
                            setattr(existing_invoice, key, value)
                    updated_count += 1
                else:
                    # Create new invoice
                    new_invoice = Invoice(**invoice_data)
                    db.session.add(new_invoice)
                    created_count += 1

            db.session.commit()

            # CRITIQUE: Re-vérifier l'arrêt manuel après le traitement pour éviter la race condition
            if sync_log and not manual_stop_requested:
                db.session.refresh(sync_log)
                if sync_log.is_stop_requested():
                    manual_stop_requested = True
                    logging.warning(f"🛑 Arrêt manuel détecté en finalisation pour QuickBooks sync_invoices {sync_log_id}")

            # Mettre à jour le SyncLog final
            if sync_log:
                if manual_stop_requested:
                    sync_log.acknowledge_stop()
                    logging.warning(f"🛑 QuickBooks sync_invoices arrêtée manuellement après {created_count} créations, {updated_count} mises à jour")
                else:
                    sync_log.status = 'completed'
                    sync_log.completed_at = datetime.utcnow()
                    sync_log.invoices_synced = created_count + updated_count
                db.session.commit()

            # Incrémenter le compteur de synchronisation après succès (sauf si arrêt manuel)
            if not manual_stop_requested:
                CompanySyncUsage.increment_company_sync_count(company_id)

        except Exception as e:
            logging.error(f"Error syncing invoices from QuickBooks: {e}")
            if sync_log:
                sync_log.status = 'failed'
                sync_log.error_message = str(e)
                sync_log.completed_at = datetime.utcnow()
                db.session.commit()
            db.session.rollback()
            raise

        return created_count, updated_count

    def _map_customer_fields(self, qb_customer: Dict, field_mapping: Dict) -> Dict:
        """Map QuickBooks customer fields to local client fields"""
        client_data = {}

        # Map basic fields avec valeurs par défaut pour éviter les erreurs
        client_data['code_client'] = self._get_nested_field(qb_customer, field_mapping.get('client_code', 'Name')) or qb_customer.get('Id', f"QB_{qb_customer.get('Id', 'unknown')}")
        client_data['name'] = self._get_nested_field(qb_customer, field_mapping.get('client_name', 'DisplayName')) or qb_customer.get('Name', 'Client sans nom')
        client_data['email'] = self._get_nested_field(qb_customer, field_mapping.get('client_email', 'PrimaryEmailAddr.Address'))
        client_data['phone'] = self._get_nested_field(qb_customer, field_mapping.get('client_phone', 'PrimaryPhone.FreeFormNumber'))

        # Handle address mapping
        address_field = field_mapping.get('client_address', 'BillAddr')
        if address_field in qb_customer and qb_customer[address_field]:
            addr = qb_customer[address_field]
            address_parts = []
            for line_key in ['Line1', 'Line2', 'Line3', 'Line4', 'Line5']:
                if line_key in addr and addr[line_key]:
                    address_parts.append(addr[line_key])
            client_data['address'] = '\n'.join(address_parts)

        # S'assurer que code_client n'est jamais vide
        if not client_data['code_client']:
            client_data['code_client'] = f"QB_{qb_customer.get('Id', 'unknown')}"

        # Ajouter la langue par défaut (français) pour les clients QuickBooks
        client_data['language'] = 'fr'

        # Note: Le modèle Client n'a pas de champ is_active, donc on ne mappe pas Active
        # Les clients QuickBooks sont considérés comme actifs par défaut s'ils sont dans le système

        # Gérer la hiérarchie parent/enfant selon la documentation QuickBooks
        job_flag = qb_customer.get('Job', False)
        parent_ref = qb_customer.get('ParentRef')


        if parent_ref and job_flag:
            # C'est un sub-customer ou job - chercher le parent par ID ou nom
            parent_id = parent_ref.get('value')
            parent_name = parent_ref.get('name')

            if parent_id:
                # On stocke l'ID du parent pour traitement en seconde phase
                client_data['_parent_id'] = parent_id
            elif parent_name:
                # On stocke le nom du parent pour traitement en seconde phase
                client_data['_parent_name'] = parent_name

        # Ajouter les nouveaux champs par défaut
        # Note: is_parent est une propriété calculée, ne pas l'assigner
        client_data['parent_client_id'] = None  # Sera défini en seconde phase si nécessaire

        # Clean up None values sauf pour les champs obligatoires
        result = {}
        for k, v in client_data.items():
            if k in ['code_client', 'name', 'language', 'parent_client_id'] or v is not None:
                result[k] = v
        return result

    def _map_invoice_fields(self, qb_invoice: Dict, field_mapping: Dict, company_id: int) -> Optional[Dict]:
        """Map QuickBooks invoice fields to local invoice fields"""
        from models import Client

        print(f"=== MAPPING INVOICE {qb_invoice.get('DocNumber', 'N/A')} ===")

        # Find the client by QuickBooks customer reference
        customer_ref = qb_invoice.get('CustomerRef', {})
        customer_name = customer_ref.get('name')
        customer_id = customer_ref.get('value')

        print(f"Customer ref: {customer_ref}")
        print(f"Customer name: {customer_name}, Customer ID: {customer_id}")

        if not customer_name:
            print("No customer name found, skipping invoice")
            return None

        # Find local client by code_client (mapped from QuickBooks customer name)
        client = Client.query.filter_by(
            code_client=customer_name,
            company_id=company_id
        ).first()

        # Try to find by name if not found by code_client
        if not client:
            client = Client.query.filter_by(
                name=customer_name,
                company_id=company_id
            ).first()

        if not client:
            print(f"Client '{customer_name}' not found in Finova AR database")
            return None

        print(f"Found matching client: {client.name} (ID: {client.id})")

        # Get amounts - TotalAmt is the original invoice amount, Balance is the unpaid amount
        balance = float(self._get_nested_field(qb_invoice, field_mapping.get('invoice_amount', 'Balance')) or 0)
        total_amt = self._get_nested_field(qb_invoice, 'TotalAmt')
        original_amount = float(total_amt) if total_amt is not None else None

        invoice_data = {
            'client_id': client.id,
            'company_id': company_id,
            'invoice_number': self._get_nested_field(qb_invoice, field_mapping.get('invoice_number', 'DocNumber')),
            'amount': balance,
            'original_amount': original_amount,
            'invoice_date': self._parse_date(self._get_nested_field(qb_invoice, field_mapping.get('invoice_date', 'TxnDate'))),
            'due_date': self._parse_date(self._get_nested_field(qb_invoice, field_mapping.get('invoice_due_date', 'DueDate'))),
            'invoice_id_external': qb_invoice.get('Id'),  # Store QuickBooks Invoice ID for PDF download

        }

        return invoice_data

    def _get_nested_field(self, data: Dict, field_path: str):
        """Get nested field value using dot notation (e.g., 'PrimaryEmailAddr.Address')"""
        if not field_path or not data:
            return None

        keys = field_path.split('.')
        value = data

        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None

        return value

    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse QuickBooks date string to datetime object"""
        if not date_str:
            return None

        try:
            # QuickBooks typically uses YYYY-MM-DD format
            return datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            try:
                # Try alternative format
                return datetime.strptime(date_str, '%m/%d/%Y').date()
            except ValueError:
                return None

    def sync_payments(self, company_id: int, sync_log_id: Optional[int] = None) -> int:
        """Synchronise les paiements reçus depuis QuickBooks vers la table ReceivedPayment.

        Stratégie : interroger les objets Payment QB (TxnDate = date de paiement),
        puis résoudre les métadonnées de chaque facture liée (DocNumber, TxnDate, DueDate)
        via une requête batch. Mapping client par QB_{CustomerRef.value} ou DisplayName.

        Returns:
            int: Nombre d'enregistrements ReceivedPayment créés
        """
        logger.info("=== STARTING QB PAYMENT SYNC ===")
        created_count = 0

        try:
            from app import db
            from models import Client, ReceivedPayment

            if not self.connection:
                raise ValueError("No QuickBooks connection configured")

            if self.connection.company_id != company_id:
                raise ValueError(
                    f"SÉCURITÉ: Cross-company sync attempt. "
                    f"Connection belongs to company {self.connection.company_id} "
                    f"but sync requested for company {company_id}"
                )

            if not self.client_id or not self.client_secret:
                raise Exception("QuickBooks credentials not configured")

            clients_by_qb_id = {}
            clients_by_name = {}
            for client in Client.query.filter_by(company_id=company_id).all():
                if not client.code_client:
                    continue
                if client.code_client.startswith('QB_'):
                    qb_id = client.code_client[3:]
                    clients_by_qb_id[qb_id] = client
                else:
                    clients_by_name[client.code_client] = client

            logger.info(
                f"QB payment sync: {len(clients_by_qb_id)} clients by QB ID, "
                f"{len(clients_by_name)} by name"
            )

            existing_keys = set(
                f"{rp.external_payment_id}|{rp.invoice_number}"
                for rp in ReceivedPayment.query.filter_by(
                    company_id=company_id,
                    source='quickbooks'
                ).with_entities(
                    ReceivedPayment.external_payment_id,
                    ReceivedPayment.invoice_number
                ).all()
            )

            last_sync_row = (
                db.session.query(db.func.max(ReceivedPayment.created_at))
                .filter_by(company_id=company_id, source='quickbooks')
                .scalar()
            )

            if last_sync_row:
                cutoff_date = (last_sync_row - timedelta(days=1)).strftime('%Y-%m-%d')
                date_filter = f" WHERE TxnDate >= '{cutoff_date}'"
                logger.info(f"Incremental QB payment sync from {cutoff_date}")
            else:
                date_filter = ""
                logger.info("Full historical QB payment sync")

            start_pos = 1
            max_results = 500

            while True:
                query = (
                    f"SELECT * FROM Payment{date_filter} "
                    f"STARTPOSITION {start_pos} MAXRESULTS {max_results}"
                )
                response = self.make_api_request('query', params={'query': query})
                qr = response.get('QueryResponse', {})
                payments_batch = qr.get('Payment', [])

                if not payments_batch:
                    break

                logger.info(
                    f"QB payments batch offset={start_pos}: {len(payments_batch)} payments"
                )

                invoice_id_set = set()
                for payment in payments_batch:
                    for line in payment.get('Line', []):
                        for linked in line.get('LinkedTxn', []):
                            if linked.get('TxnType') == 'Invoice':
                                invoice_id_set.add(str(linked['TxnId']))

                invoice_details = {}
                if invoice_id_set:
                    ids_csv = "', '".join(invoice_id_set)
                    inv_query = (
                        f"SELECT Id, DocNumber, TxnDate, DueDate "
                        f"FROM Invoice WHERE Id IN ('{ids_csv}')"
                    )
                    try:
                        inv_response = self.make_api_request(
                            'query', params={'query': inv_query}
                        )
                        inv_qr = inv_response.get('QueryResponse', {})
                        for inv in inv_qr.get('Invoice', []):
                            invoice_details[str(inv['Id'])] = inv
                    except Exception as inv_err:
                        logger.warning(f"QB invoice batch fetch error: {inv_err}")

                for payment in payments_batch:
                    payment_id = str(payment.get('Id', ''))
                    payment_date_raw = payment.get('TxnDate')
                    customer_ref = payment.get('CustomerRef', {})
                    customer_id = str(customer_ref.get('value', ''))
                    customer_name = customer_ref.get('name', '')

                    client = clients_by_qb_id.get(customer_id) or clients_by_name.get(customer_name)
                    if not client:
                        continue

                    payment_date = self._parse_date(payment_date_raw)
                    if not payment_date:
                        continue

                    for line in payment.get('Line', []):
                        for linked in line.get('LinkedTxn', []):
                            if linked.get('TxnType') != 'Invoice':
                                continue

                            inv_id = str(linked['TxnId'])
                            inv = invoice_details.get(inv_id)
                            if not inv:
                                continue

                            invoice_number = inv.get('DocNumber', '')
                            if not invoice_number:
                                continue

                            dedup_key = f"{payment_id}|{invoice_number}"
                            if dedup_key in existing_keys:
                                continue

                            invoice_date = self._parse_date(inv.get('TxnDate'))
                            invoice_due_date = self._parse_date(inv.get('DueDate'))

                            amount_line = float(line.get('Amount', 0) or 0)
                            amount_total = float(payment.get('TotalAmt', amount_line) or amount_line)

                            rp = ReceivedPayment(
                                company_id=company_id,
                                client_id=client.id,
                                invoice_number=invoice_number,
                                invoice_date=invoice_date,
                                invoice_due_date=invoice_due_date,
                                original_invoice_amount=amount_total,
                                payment_date=payment_date,
                                payment_amount=amount_line if amount_line > 0 else amount_total,
                                source='quickbooks',
                                external_payment_id=payment_id,
                                external_invoice_id=inv_id
                            )
                            db.session.add(rp)
                            existing_keys.add(dedup_key)
                            created_count += 1

                try:
                    db.session.commit()
                except Exception as commit_err:
                    logger.warning(f"QB payment batch commit error: {commit_err}")
                    db.session.rollback()

                if len(payments_batch) < max_results:
                    break

                start_pos += max_results

            logger.info(f"=== QB PAYMENT SYNC COMPLETE: {created_count} records created ===")
            return created_count

        except Exception as e:
            logger.error(f"Error during QB payment sync: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass
            return created_count

    def perform_full_sync(self, company_id: int) -> Dict:
        """Perform a full synchronization of customers and invoices"""
        sync_log = SyncLog(
            connection_id=self.connection.id,
            sync_type='manual',
            status='running'
        )
        db.session.add(sync_log)
        db.session.commit()

        try:
            # Test connection first
            is_connected, message = self.test_connection()
            if not is_connected:
                raise Exception(message)

            # Sync customers
            customers_created, customers_updated = self.sync_customers(company_id)

            # Sync invoices
            invoices_created, invoices_updated = self.sync_invoices(company_id)

            # Update sync log
            sync_log.status = 'completed'
            sync_log.clients_synced = customers_created + customers_updated
            sync_log.invoices_synced = invoices_created + invoices_updated
            sync_log.completed_at = datetime.utcnow()

            # Update connection last sync time
            self.connection.last_sync_at = datetime.utcnow()

            db.session.commit()

            # Enregistrer snapshot des CAR (non bloquant)
            try:
                from utils.receivables_snapshot import create_receivables_snapshot
                create_receivables_snapshot(company_id, trigger_type='sync')
            except Exception as snapshot_error:
                logger.warning(f"Snapshot CAR non créé: {snapshot_error}")

            return {
                'success': True,
                'customers_created': customers_created,
                'customers_updated': customers_updated,
                'invoices_created': invoices_created,
                'invoices_updated': invoices_updated,
                'message': f'Synchronisation réussie: {customers_created + customers_updated} clients, {invoices_created + invoices_updated} factures'
            }

        except Exception as e:
            # Update sync log with error
            sync_log.status = 'failed'
            sync_log.error_message = str(e)

    def sync_all_data(self) -> tuple:
        """Simplified sync method that returns (customers_count, invoices_count)"""
        try:
            company_id = self.connection.company_id

            # Sync customers
            customers_created, customers_updated = self.sync_customers(company_id)

            # Sync invoices
            invoices_created, invoices_updated = self.sync_invoices(company_id)

            # Update connection last sync time
            self.connection.last_sync_at = datetime.utcnow()
            db.session.commit()

            total_customers = customers_created + customers_updated
            total_invoices = invoices_created + invoices_updated

            return (total_customers, total_invoices)

        except Exception as e:
            logging.error(f"Error in sync_all_data: {e}")
            db.session.rollback()
            raise e

    def download_invoice_pdf(self, invoice_id_external: str) -> bytes:
        """Download invoice PDF from QuickBooks

        Args:
            invoice_id_external: The QuickBooks invoice ID

        Returns:
            bytes: The PDF file content

        Raises:
            ValueError: If connection is not available or token is invalid
            requests.HTTPError: If API request fails
        """
        if not self.connection:
            raise ValueError("No QuickBooks connection available")

        # Check if token needs refreshing
        if not self.connection.is_token_valid():
            if not self.refresh_access_token():
                raise ValueError("Failed to refresh QuickBooks access token")

        # Build PDF download URL
        base_url = f"{self.get_discovery_url()}/v3/company/{self.connection.company_id_external}"
        pdf_url = f"{base_url}/invoice/{invoice_id_external}/pdf"

        headers = {
            'Authorization': f'Bearer {self.connection.access_token}',
            'Accept': 'application/pdf'
        }

        params = {'minorversion': '75'}

        # Use robust HTTP session
        from utils.http_client import create_quickbooks_session

        session = create_quickbooks_session()
        response = session.get(pdf_url, headers=headers, params=params)

        # Return PDF bytes
        return response.content


def get_quickbooks_connector(company_id: int) -> Optional[QuickBooksConnector]:
    """Get QuickBooks connector for a company"""
    connection = AccountingConnection.query.filter_by(
        company_id=company_id,
        system_type='quickbooks',
        is_active=True
    ).first()

    if connection:
        connector = QuickBooksConnector(connection.id, company_id)
        # Set credentials from environment or configuration
        # In production, these should come from environment variables
        connector.client_id = current_app.config.get('QUICKBOOKS_CLIENT_ID')
        connector.client_secret = current_app.config.get('QUICKBOOKS_CLIENT_SECRET')
        return connector

    return None