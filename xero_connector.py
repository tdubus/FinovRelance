"""
Xero Accounting Connector
Handles OAuth 2.0 authentication and data synchronization with Xero API

SÉCURITÉ ET STANDARDS:
- OAuth 2.0 avec gestion des tokens rotatifs (refresh token change à chaque refresh)
- Circuit breaker pour protection API
- RobustHTTPSession avec retry automatique
- Encryption AES-256 des tokens
- Company isolation stricte (protection IDOR)
- Vérification limites de plan avant import
"""

import json
import logging
import requests
import base64
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode
from flask import current_app, url_for
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from utils.circuit_breaker import CircuitState, CircuitBreaker

logger = logging.getLogger(__name__)


class XeroConnector:
    """Xero Accounting API connector with OAuth 2.0"""

    # Xero OAuth 2.0 endpoints
    AUTHORIZATION_URL = 'https://login.xero.com/identity/connect/authorize'
    TOKEN_URL = 'https://identity.xero.com/connect/token'
    CONNECTIONS_URL = 'https://api.xero.com/connections'
    API_BASE_URL = 'https://api.xero.com/api.xro/2.0'

    # Required scopes for Xero API
    SCOPES = 'openid profile email accounting.transactions accounting.contacts accounting.settings offline_access'

    def __init__(self, connection_id: Optional[int] = None, company_id: Optional[int] = None):
        """Initialize connector with optional existing connection

        Args:
            connection_id: ID of the Xero connection
            company_id: ID of the company that owns the connection (for security validation)
        """
        import os

        self.connection = None
        if connection_id:
            from models import AccountingConnection
            self.connection = AccountingConnection.query.get(connection_id)

            # SÉCURITÉ CRITIQUE: Vérifier que la connexion appartient à l'entreprise spécifiée
            if self.connection and company_id and self.connection.company_id != company_id:
                raise ValueError(
                    f"SÉCURITÉ: Tentative d'accès à une connexion Xero non autorisée. "
                    f"Connection {connection_id} n'appartient pas à l'entreprise {company_id}"
                )

            if self.connection and not company_id:
                logging.warning(
                    f"SÉCURITÉ: Connexion Xero {connection_id} chargée sans validation de l'entreprise"
                )

        # Load credentials from environment variables
        self.client_id = os.environ.get('XERO_CLIENT_ID')
        self.client_secret = os.environ.get('XERO_CLIENT_SECRET')

        # Initialize circuit breaker for API calls (same as Business Central)
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)

        # Initialize thread pool for parallel processing (max 2 workers)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="xero_sync")

        # Lock for thread-safe operations
        self.db_lock = Lock()

    def get_authorization_url(self, state: str) -> str:
        """Generate OAuth authorization URL for Xero"""
        if not self.client_id:
            raise ValueError("Xero Client ID not configured")

        redirect_uri = url_for('company.xero_callback', _external=True)

        params = {
            'response_type': 'code',
            'client_id': self.client_id,
            'redirect_uri': redirect_uri,
            'scope': self.SCOPES,
            'state': state
        }

        authorization_url = f"{self.AUTHORIZATION_URL}?{urlencode(params)}"
        return authorization_url

    def exchange_code_for_tokens(self, authorization_code: str, state: str) -> Dict:
        """Exchange authorization code for access and refresh tokens

        Returns dict with: access_token, refresh_token, expires_in, tenant_id
        """
        if not self.client_id or not self.client_secret:
            raise ValueError("Xero credentials not configured")

        redirect_uri = url_for('company.xero_callback', _external=True)

        # Prepare token request
        data = {
            'grant_type': 'authorization_code',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code': authorization_code,
            'redirect_uri': redirect_uri
        }

        try:
            # Exchange code for tokens
            response = requests.post(self.TOKEN_URL, data=data, timeout=30)
            response.raise_for_status()
            token_data = response.json()

            # IMPORTANT: Get tenant ID (organization ID) from connections endpoint
            access_token = token_data['access_token']
            tenant_id = self._get_tenant_id(access_token)

            if not tenant_id:
                raise ValueError("Failed to retrieve Xero tenant ID")

            # Return complete token data including tenant_id
            return {
                'access_token': token_data['access_token'],
                'refresh_token': token_data['refresh_token'],
                'expires_in': token_data.get('expires_in', 1800),  # 30 minutes
                'tenant_id': tenant_id
            }

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to exchange Xero authorization code: {e}")
            raise ValueError(f"Failed to obtain Xero tokens: {str(e)}")

    def _get_tenant_id(self, access_token: str) -> Optional[str]:
        """Get Xero tenant ID (organization ID) using the connections endpoint"""
        try:
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }

            response = requests.get(self.CONNECTIONS_URL, headers=headers, timeout=30)
            response.raise_for_status()

            connections = response.json()
            if connections and len(connections) > 0:
                # Get the first tenant ID (organization)
                return connections[0]['tenantId']

            return None

        except Exception as e:
            logger.error(f"Failed to get Xero tenant ID: {e}")
            return None

    def refresh_access_token(self) -> bool:
        """Refresh the access token using refresh token

        IMPORTANT: Xero uses ROTATING refresh tokens - the old refresh token
        becomes invalid after use, and a new one is returned.
        """
        if not self.connection or not self.connection.refresh_token:
            return False

        if not self.client_id or not self.client_secret:
            raise ValueError("Xero credentials not configured")

        data = {
            'grant_type': 'refresh_token',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': self.connection.refresh_token
        }

        try:
            response = requests.post(self.TOKEN_URL, data=data, timeout=30)
            response.raise_for_status()

            token_data = response.json()

            # Update connection with new tokens
            self.connection.access_token = token_data['access_token']

            # CRITICAL: Update refresh token (Xero uses rotating tokens)
            if 'refresh_token' in token_data:
                self.connection.refresh_token = token_data['refresh_token']

            self.connection.token_expires_at = datetime.utcnow() + timedelta(
                seconds=token_data.get('expires_in', 1800)
            )

            from app import db
            db.session.commit()

            logger.info(f"Successfully refreshed Xero access token for connection {self.connection.id}")
            return True

        except Exception as e:
            logger.error(f"Failed to refresh Xero token: {e}")
            return False

    def make_api_request(self, endpoint: str, method: str = 'GET', params: Dict = None, json_data: Dict = None) -> Dict:
        """Make authenticated API request to Xero

        Args:
            endpoint: API endpoint (e.g., 'Contacts', 'Invoices')
            method: HTTP method (GET, POST, etc.)
            params: Query parameters
            json_data: JSON body for POST/PUT requests
        """
        if not self.connection:
            raise ValueError("No Xero connection available")

        # Check if token needs refreshing (with 5 minute buffer)
        if not self.connection.is_token_valid():
            if not self.refresh_access_token():
                raise ValueError("Failed to refresh Xero access token")

        # Build full URL
        url = f"{self.API_BASE_URL}/{endpoint}"

        headers = {
            'Authorization': f'Bearer {self.connection.access_token}',
            'Xero-Tenant-Id': self.connection.company_id_external,  # Tenant ID stored in company_id_external
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }

        # Use robust HTTP session
        from utils.http_client import create_xero_session

        session = create_xero_session()
        response = session.request(method, url, headers=headers, params=params, json=json_data)

        return response.json()

    def test_connection(self) -> Tuple[bool, str]:
        """Test the Xero connection"""
        try:
            # Try to fetch organisation info
            response = self.make_api_request('Organisation')

            organisations = response.get('Organisations', [])
            if organisations:
                org_name = organisations[0].get('Name', 'Xero')
                return True, f"Connexion réussie à {org_name}"
            else:
                return False, "Impossible de récupérer les informations de l'organisation"

        except Exception as e:
            return False, f"Erreur de connexion: {str(e)}"

    def sync_customers(self, company_id: int, sync_log_id: Optional[int] = None) -> Tuple[int, int]:
        """Sync customers (Contacts) from Xero to local database

        Args:
            company_id: ID of the company to sync data for
            sync_log_id: Optional ID of the SyncLog entry for tracking

        Returns:
            Tuple of (created_count, updated_count)
        """
        created_count = 0
        updated_count = 0
        manual_stop_requested = False
        sync_log = None

        try:
            from app import db
            from models import Client, CompanySyncUsage, SyncLog

            # Load SyncLog if provided
            if sync_log_id:
                sync_log = SyncLog.query.get(sync_log_id)
                if sync_log:
                    sync_log.status = 'running'
                    db.session.commit()

            # SÉCURITÉ CRITIQUE: Vérifier l'intégrité des données
            if not self.connection:
                raise ValueError("Aucune connexion Xero configurée")

            if self.connection.company_id != company_id:
                raise ValueError(
                    f"SÉCURITÉ: Tentative de synchronisation croisée détectée. "
                    f"Connexion appartient à l'entreprise {self.connection.company_id} "
                    f"mais tentative de sync pour l'entreprise {company_id}"
                )

            # Vérifier les limites de synchronisation
            if not CompanySyncUsage.check_company_sync_limit(company_id):
                raise Exception("Limite quotidienne de synchronisation atteinte pour votre forfait.")

            # Fetch ALL contacts from Xero with pagination
            all_contacts = []
            page = 1
            page_size = 1000  # Xero supports up to 1000 items per page (2025 update)

            while True:
                # Check for manual stop
                if sync_log:
                    db.session.refresh(sync_log)
                    if sync_log.is_stop_requested():
                        manual_stop_requested = True
                        logger.warning(f"🛑 Arrêt manuel demandé pour Xero sync_customers (SyncLog {sync_log_id})")
                        break

                params = {
                    'page': page,
                    'where': 'IsCustomer==true'  # Only fetch customers
                }

                response = self.make_api_request('Contacts', params=params)
                contacts_batch = response.get('Contacts', [])

                if not contacts_batch:
                    break

                all_contacts.extend(contacts_batch)

                # Check if there are more pages
                if len(contacts_batch) < page_size:
                    break

                page += 1

            logger.info(f"Total clients récupérés de Xero: {len(all_contacts)}")

            # SÉCURITÉ LICENCE: Compter les nouveaux clients avant création
            from models import Company
            company = Company.query.get(company_id)
            if not company:
                raise ValueError(f"Entreprise {company_id} non trouvée")

            new_clients_count = 0
            for xero_contact in all_contacts:
                client_data = self._map_contact_fields(xero_contact)
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
                    logger.info(
                        f"✅ Xero sync autorisé: {new_clients_count} nouveaux clients "
                        f"(plan: {company.get_plan_display_name()})"
                    )
                except ValueError as e:
                    raise Exception(f"🚫 Import Xero bloqué: {str(e)}")

            # Create/update clients
            for xero_contact in all_contacts:
                client_data = self._map_contact_fields(xero_contact)

                existing_client = Client.query.filter_by(
                    code_client=client_data['code_client'],
                    company_id=company_id
                ).first()

                if existing_client:
                    # Update existing client
                    for key, value in client_data.items():
                        if value is not None and key != 'code_client':
                            setattr(existing_client, key, value)
                    updated_count += 1
                else:
                    # Create new client
                    new_client = Client(
                        company_id=company_id,
                        **client_data
                    )
                    db.session.add(new_client)
                    created_count += 1

            # Enregistrer l'utilisation de synchronisation
            if not manual_stop_requested:
                CompanySyncUsage.increment_company_sync_count(company_id)

            db.session.commit()

        except Exception as e:
            logger.error(f"Error syncing Xero customers: {e}")
            db.session.rollback()
            raise

        return created_count, updated_count

    def sync_invoices(self, company_id: int, sync_log_id: Optional[int] = None) -> Tuple[int, int]:
        """Sync invoices from Xero to local database

        Only syncs unpaid invoices (AmountDue > 0)
        Deletes locally paid invoices (clean slate approach)

        Args:
            company_id: ID of the company to sync data for
            sync_log_id: Optional ID of the SyncLog entry for tracking

        Returns:
            Tuple of (created_count, updated_count)
        """
        created_count = 0
        updated_count = 0
        manual_stop_requested = False
        sync_log = None

        try:
            from app import db
            from models import Invoice, Client, CompanySyncUsage, SyncLog

            # Load SyncLog if provided
            if sync_log_id:
                sync_log = SyncLog.query.get(sync_log_id)
                if sync_log:
                    sync_log.status = 'running'
                    db.session.commit()

            # SÉCURITÉ CRITIQUE
            if not self.connection:
                raise ValueError("Aucune connexion Xero configurée")

            if self.connection.company_id != company_id:
                raise ValueError(
                    f"SÉCURITÉ: Tentative de synchronisation croisée détectée. "
                    f"Connexion appartient à l'entreprise {self.connection.company_id} "
                    f"mais tentative de sync pour l'entreprise {company_id}"
                )

            # Vérifier les limites de synchronisation
            if not CompanySyncUsage.check_company_sync_limit(company_id):
                raise Exception("Limite quotidienne de synchronisation atteinte pour votre forfait.")

            # Fetch ALL invoices from Xero with pagination
            all_invoices = []
            page = 1
            page_size = 1000  # Xero supports up to 1000 items per page (2025 update)

            while True:
                # Check for manual stop
                if sync_log:
                    db.session.refresh(sync_log)
                    if sync_log.is_stop_requested():
                        manual_stop_requested = True
                        logger.warning(f"🛑 Arrêt manuel demandé pour Xero sync_invoices (SyncLog {sync_log_id})")
                        break

                params = {
                    'page': page,
                    'where': 'Status=="AUTHORISED" OR Status=="SUBMITTED"',  # Only unpaid/authorized invoices
                    'order': 'Date DESC'
                }

                response = self.make_api_request('Invoices', params=params)
                invoices_batch = response.get('Invoices', [])

                if not invoices_batch:
                    break

                all_invoices.extend(invoices_batch)

                if len(invoices_batch) < page_size:
                    break

                page += 1

            logger.info(f"Total factures récupérées de Xero: {len(all_invoices)}")

            # Process invoices
            for xero_invoice in all_invoices:
                # Check amount due
                amount_due = float(xero_invoice.get('AmountDue', 0))

                # Delete fully paid invoices (clean slate approach)
                if amount_due <= 0:
                    invoice_number = xero_invoice.get('InvoiceNumber')
                    existing_paid_invoice = Invoice.query.filter_by(
                        invoice_number=invoice_number,
                        company_id=company_id
                    ).first()
                    if existing_paid_invoice:
                        db.session.delete(existing_paid_invoice)
                        logger.info(f"Deleted paid invoice {invoice_number} from local database")
                    continue

                # Map invoice fields
                invoice_data = self._map_invoice_fields(xero_invoice, company_id)

                if not invoice_data:
                    continue

                existing_invoice = Invoice.query.filter_by(
                    invoice_number=invoice_data['invoice_number'],
                    company_id=company_id
                ).first()

                if existing_invoice:
                    # Update existing invoice
                    for key, value in invoice_data.items():
                        if value is not None and key != 'invoice_number':
                            setattr(existing_invoice, key, value)
                    updated_count += 1
                else:
                    # Create new invoice
                    new_invoice = Invoice(**invoice_data)
                    db.session.add(new_invoice)
                    created_count += 1

            # Enregistrer l'utilisation de synchronisation
            if not manual_stop_requested:
                CompanySyncUsage.increment_company_sync_count(company_id)

            db.session.commit()

        except Exception as e:
            logger.error(f"Error syncing Xero invoices: {e}")
            db.session.rollback()
            raise

        return created_count, updated_count

    def _map_contact_fields(self, xero_contact: Dict) -> Dict:
        """Map Xero contact fields to local client fields (hardcoded mapping)"""
        client_data = {}

        # Required fields
        # Priority: AccountNumber (user-editable) > ContactNumber (API-only) > Generated from Name
        account_number = xero_contact.get('AccountNumber')
        contact_number = xero_contact.get('ContactNumber')
        contact_name = xero_contact.get('Name', 'Client sans nom')

        # CRITICAL: Filter out 'None' string and empty values
        # Xero API returns string 'None' instead of Python None for empty fields
        if account_number in ('None', None, ''):
            account_number = None
        if contact_number in ('None', None, ''):
            contact_number = None

        if account_number:
            client_data['code_client'] = account_number
        elif contact_number:
            client_data['code_client'] = contact_number
        else:
            # Generate code from name (first 10 chars + random suffix)
            import re
            clean_name = re.sub(r'[^a-zA-Z0-9]', '', contact_name)[:10].upper()
            client_data['code_client'] = f"{clean_name}_{xero_contact.get('ContactID', 'unknown')[:8]}"

        client_data['name'] = contact_name

        # Optional fields
        client_data['email'] = xero_contact.get('EmailAddress')

        # Phone - get first phone if available
        phones = xero_contact.get('Phones', [])
        if phones:
            client_data['phone'] = phones[0].get('PhoneNumber')

        # Address - get first postal address
        addresses = xero_contact.get('Addresses', [])
        for address in addresses:
            if address.get('AddressType') == 'POBOX' or address.get('AddressType') == 'STREET':
                address_parts = []
                for field in ['AddressLine1', 'AddressLine2', 'AddressLine3', 'AddressLine4']:
                    if address.get(field):
                        address_parts.append(address[field])
                if address.get('City'):
                    address_parts.append(address['City'])
                if address.get('PostalCode'):
                    address_parts.append(address['PostalCode'])
                if address.get('Country'):
                    address_parts.append(address['Country'])
                client_data['address'] = ', '.join(address_parts)
                break

        # Default language
        client_data['language'] = 'fr'

        return client_data

    def _map_invoice_fields(self, xero_invoice: Dict, company_id: int) -> Optional[Dict]:
        """Map Xero invoice fields to local invoice fields (hardcoded mapping)"""
        from models import Client

        # Get contact name to find matching client
        contact = xero_invoice.get('Contact', {})
        contact_name = contact.get('Name', '')
        contact_number = contact.get('ContactNumber', '')
        account_number = contact.get('AccountNumber')
        contact_id = contact.get('ContactID', '')

        # CRITICAL: Filter out 'None' string and empty values
        # Xero API returns string 'None' instead of Python None for empty fields
        if account_number in ('None', None, ''):
            account_number = None
        if contact_number in ('None', None, ''):
            contact_number = None

        # DEBUG: Log what we received from Xero API

        # Try to find client by AccountNumber, ContactNumber, or name
        client = None

        if account_number:
            client = Client.query.filter_by(
                code_client=account_number,
                company_id=company_id
            ).first()

        if not client and contact_number:
            client = Client.query.filter_by(
                code_client=contact_number,
                company_id=company_id
            ).first()

        # Try by name if not found
        if not client and contact_name:
            client = Client.query.filter_by(
                name=contact_name,
                company_id=company_id
            ).first()

        if not client:
            # List all clients to help debug
            all_clients = Client.query.filter_by(company_id=company_id).limit(10).all()
            logger.warning(f"❌ Client '{contact_name}' not found in Finova AR database")
            logger.warning(f"📋 Available clients (first 10): {[(c.code_client, c.name) for c in all_clients]}")
            return None

        # Get amounts - Total is the original invoice amount, AmountDue is the balance
        amount_due = float(xero_invoice.get('AmountDue', 0))
        total = xero_invoice.get('Total')
        original_amount = float(total) if total is not None else None

        invoice_data = {
            'client_id': client.id,
            'company_id': company_id,
            'invoice_number': xero_invoice.get('InvoiceNumber'),
            'amount': amount_due,
            'original_amount': original_amount,
            'invoice_date': self._parse_date(xero_invoice.get('Date')),
            'due_date': self._parse_date(xero_invoice.get('DueDate')),
            'invoice_id_external': xero_invoice.get('InvoiceID'),  # Store Xero Invoice ID for PDF download
        }

        return invoice_data

    def download_invoice_pdf(self, invoice_id_external: str) -> bytes:
        """Download invoice PDF from Xero

        Args:
            invoice_id_external: The Xero invoice ID (InvoiceID GUID)

        Returns:
            bytes: The PDF file content

        Raises:
            ValueError: If connection is not available or token is invalid
            requests.HTTPError: If API request fails
        """
        if not self.connection:
            raise ValueError("Aucune connexion Xero configurée")

        # Validate and refresh token if needed
        if not self.connection.is_token_valid():
            logger.info("Xero token expired, refreshing before PDF download...")
            if not self.refresh_access_token():
                raise ValueError("Échec du rafraîchissement du token Xero")

        # Build PDF download URL - Same endpoint as GET invoice but with Accept: application/pdf
        url = f"{self.API_BASE_URL}/Invoices/{invoice_id_external}"

        headers = {
            'Authorization': f'Bearer {self.connection.access_token}',
            'xero-tenant-id': self.connection.company_id_external,
            'Accept': 'application/pdf'  # Critical: Forces PDF response instead of JSON
        }

        # Use robust HTTP session
        from utils.http_client import create_xero_session

        session = create_xero_session()
        response = session.get(url, headers=headers)

        # Validate response status
        if response.status_code != 200:
            logger.error(f"Xero PDF download failed: HTTP {response.status_code}")
            response.raise_for_status()  # Raises HTTPError with proper error message

        # Validate content type to ensure we got a PDF, not JSON error
        content_type = response.headers.get('Content-Type', '')
        if 'application/pdf' not in content_type:
            logger.error(f"Expected PDF but got Content-Type: {content_type}")
            # Try to parse error message if it's JSON
            try:
                error_data = response.json()
                error_msg = error_data.get('Message', 'Réponse invalide de Xero')
            except:
                error_msg = "Réponse invalide de Xero (attendu: PDF)"
            raise ValueError(error_msg)

        # Return PDF bytes
        return response.content

    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse Xero date string to datetime object

        Xero uses format: /Date(1234567890000+0000)/
        """
        if not date_str:
            return None

        try:
            # Xero date format: /Date(timestamp in milliseconds)/
            if date_str.startswith('/Date('):
                # Extract timestamp
                timestamp_str = date_str.replace('/Date(', '').replace(')/', '')
                # Remove timezone offset if present
                if '+' in timestamp_str:
                    timestamp_str = timestamp_str.split('+')[0]
                elif '-' in timestamp_str:
                    timestamp_str = timestamp_str.split('-')[0]

                # Convert from milliseconds to seconds
                timestamp = int(timestamp_str) / 1000
                return datetime.fromtimestamp(timestamp).date()

            # Fallback: try ISO format
            return datetime.strptime(date_str, '%Y-%m-%d').date()

        except (ValueError, AttributeError) as e:
            logger.warning(f"Failed to parse Xero date '{date_str}': {e}")
            return None

    def sync_payments(self, company_id: int, sync_log_id: Optional[int] = None) -> int:
        """Synchronise les paiements reçus depuis Xero vers la table ReceivedPayment.

        Stratégie : interroger toutes les factures PAID (Status=="PAID").
        FullyPaidOnDate = date de paiement. Mapping client via ContactID[:8] ou AccountNumber.
        Sync incrémentale via le header ModifiedAfter.

        Returns:
            int: Nombre d'enregistrements ReceivedPayment créés
        """
        logger.info("=== STARTING XERO PAYMENT SYNC ===")
        created_count = 0

        try:
            from app import db
            from models import Client, ReceivedPayment

            if not self.connection:
                raise ValueError("No Xero connection configured")

            if self.connection.company_id != company_id:
                raise ValueError(
                    f"SÉCURITÉ: Cross-company sync attempt. "
                    f"Connection belongs to company {self.connection.company_id} "
                    f"but sync requested for company {company_id}"
                )

            if not self.client_id or not self.client_secret:
                raise Exception("Xero credentials not configured")

            clients_by_contact_suffix = {}
            clients_by_account_number = {}
            for client in Client.query.filter_by(company_id=company_id).all():
                cc = client.code_client or ''
                if not cc:
                    continue
                if '_' in cc:
                    suffix = cc.rsplit('_', 1)[-1]
                    if len(suffix) == 8:
                        clients_by_contact_suffix[suffix] = client
                clients_by_account_number[cc] = client

            logger.info(
                f"Xero payment sync: {len(clients_by_contact_suffix)} clients by ContactID[:8], "
                f"{len(clients_by_account_number)} by code_client"
            )

            existing_keys = set(
                f"{rp.external_payment_id}|{rp.invoice_number}"
                for rp in ReceivedPayment.query.filter_by(
                    company_id=company_id,
                    source='xero'
                ).with_entities(
                    ReceivedPayment.external_payment_id,
                    ReceivedPayment.invoice_number
                ).all()
            )

            last_sync_row = (
                db.session.query(db.func.max(ReceivedPayment.created_at))
                .filter_by(company_id=company_id, source='xero')
                .scalar()
            )

            extra_params = {}
            if last_sync_row:
                cutoff = (last_sync_row - timedelta(days=1))
                extra_params['ModifiedAfter'] = cutoff.strftime('%Y-%m-%dT%H:%M:%S')
                logger.info(f"Incremental Xero payment sync from {extra_params['ModifiedAfter']}")
            else:
                logger.info("Full historical Xero payment sync")

            page = 1
            page_size = 1000

            while True:
                params = {
                    'page': page,
                    'where': 'Status=="PAID"',
                    **extra_params
                }

                response = self.make_api_request('Invoices', params=params)
                invoices_batch = response.get('Invoices', [])

                if not invoices_batch:
                    break

                logger.info(f"Xero paid invoices page {page}: {len(invoices_batch)} records")

                for inv in invoices_batch:
                    invoice_number = inv.get('InvoiceNumber', '')
                    if not invoice_number:
                        continue

                    contact = inv.get('Contact', {})
                    contact_id = contact.get('ContactID', '')
                    contact_suffix = contact_id[:8] if len(contact_id) >= 8 else ''
                    account_number = contact.get('AccountNumber', '')

                    client = (
                        clients_by_contact_suffix.get(contact_suffix)
                        or clients_by_account_number.get(account_number)
                    )
                    if not client:
                        continue

                    fully_paid_raw = inv.get('FullyPaidOnDate')
                    if not fully_paid_raw:
                        continue

                    payment_date = self._parse_date(fully_paid_raw)
                    if not payment_date:
                        continue

                    invoice_id = inv.get('InvoiceID', '')
                    external_payment_id = f"XERO_PAY_{invoice_id}"

                    dedup_key = f"{external_payment_id}|{invoice_number}"
                    if dedup_key in existing_keys:
                        continue

                    invoice_date = self._parse_date(inv.get('Date'))
                    invoice_due_date = self._parse_date(inv.get('DueDate'))
                    amount_total = float(inv.get('Total', 0) or 0)
                    amount_paid = float(inv.get('AmountPaid', amount_total) or amount_total)

                    rp = ReceivedPayment(
                        company_id=company_id,
                        client_id=client.id,
                        invoice_number=invoice_number,
                        invoice_date=invoice_date,
                        invoice_due_date=invoice_due_date,
                        original_invoice_amount=amount_total,
                        payment_date=payment_date,
                        payment_amount=amount_paid,
                        source='xero',
                        external_payment_id=external_payment_id,
                        external_invoice_id=invoice_id
                    )
                    db.session.add(rp)
                    existing_keys.add(dedup_key)
                    created_count += 1

                try:
                    db.session.commit()
                except Exception as commit_err:
                    logger.warning(f"Xero payment page commit error: {commit_err}")
                    db.session.rollback()

                if len(invoices_batch) < page_size:
                    break

                page += 1

            logger.info(f"=== XERO PAYMENT SYNC COMPLETE: {created_count} records created ===")
            return created_count

        except Exception as e:
            logger.error(f"Error during Xero payment sync: {e}")
            try:
                db.session.rollback()
            except Exception:
                pass
            return created_count

    def perform_full_sync(self, company_id: int) -> Dict:
        """Perform a full synchronization of customers and invoices"""
        from models import SyncLog
        from app import db

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
            customers_created, customers_updated = self.sync_customers(company_id, sync_log.id)

            # Sync invoices
            invoices_created, invoices_updated = self.sync_invoices(company_id, sync_log.id)

            # Update sync log
            sync_log.status = 'completed'
            sync_log.clients_synced = customers_created + customers_updated
            sync_log.invoices_synced = invoices_created + invoices_updated
            sync_log.completed_at = datetime.utcnow()

            # Update connection last sync time and updated_at for proper sorting
            self.connection.last_sync_at = datetime.utcnow()
            self.connection.updated_at = datetime.utcnow()  # Force update for sorting

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
            }

        except Exception as e:
            sync_log.status = 'failed'
            sync_log.error_message = str(e)
            sync_log.completed_at = datetime.utcnow()
            db.session.commit()

            logger.error(f"Xero full sync failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }


def get_xero_connector(company_id: int) -> Optional[XeroConnector]:
    """Get Xero connector for a company"""
    from models import AccountingConnection

    connection = AccountingConnection.query.filter_by(
        company_id=company_id,
        system_type='xero',
        is_active=True
    ).first()

    if connection:
        connector = XeroConnector(connection.id, company_id)
        return connector

    return None
