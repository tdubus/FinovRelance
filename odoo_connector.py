"""
Odoo Connector
Handles API Key authentication and data synchronization with Odoo via XML-RPC API

This connector implements:
- XML-RPC authentication with Odoo (no OAuth required)
- API Key management (encrypted storage)
- Dynamic database and URL configuration
- Field mapping and filters
- Asynchronous data synchronization
- Automatic retry and circuit breaker pattern

⚠️ IMPORTANT: DIFFÉRENCE AVEC LES AUTRES CONNECTEURS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Contrairement à QuickBooks, Xero et Business Central qui utilisent OAuth 2.0
avec des tokens qui expirent, Odoo utilise des API Keys qui sont permanentes.

❌ PAS BESOIN DE REFRESH TOKEN AUTOMATIQUE
✅ Les API Keys Odoo n'expirent pas automatiquement
✅ Pas d'ajout nécessaire dans jobs/refresh_accounting_tokens.py

⚠️ Les utilisateurs doivent régénérer manuellement l'API Key si elle est révoquée
dans Odoo (User Profile → Account Security → New API Key)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️ MODE DÉVELOPPEMENT vs PRODUCTION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Le connecteur supporte deux modes d'exécution :
- DÉVELOPPEMENT: Utilise l'URL de l'instance Odoo de développement/test
- PRODUCTION: Utilise l'URL de l'instance Odoo de production

La configuration se fait via le champ is_sandbox dans AccountingConnection:
- is_sandbox = True → Mode Développement
- is_sandbox = False → Mode Production (défaut)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import logging
from defusedxml.xmlrpc import xmlrpc_client
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from utils.circuit_breaker import CircuitState, CircuitBreaker

logger = logging.getLogger(__name__)


class OdooConnector:
    """Odoo XML-RPC API connector"""

    def __init__(self, connection_id: Optional[int] = None, company_id: Optional[int] = None):
        """Initialize connector with optional existing connection

        Args:
            connection_id: ID of the Odoo connection
            company_id: ID of the company that owns the connection (for security validation)
        """
        self.connection = None
        if connection_id:
            from models import AccountingConnection
            self.connection = AccountingConnection.query.get(connection_id)

            # SÉCURITÉ CRITIQUE: Vérifier que la connexion appartient à l'entreprise spécifiée
            if self.connection and company_id and self.connection.company_id != company_id:
                raise ValueError(
                    f"SÉCURITÉ: Tentative d'accès à une connexion Odoo non autorisée. "
                    f"Connection {connection_id} n'appartient pas à l'entreprise {company_id}"
                )

            if self.connection and not company_id:
                logging.warning(
                    f"SÉCURITÉ: Connexion Odoo {connection_id} chargée sans validation de l'entreprise"
                )

        # Circuit breaker for API calls
        self.circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=60)

        # Thread pool for parallel processing
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="odoo_sync")

        # Lock for thread-safe database operations
        self.db_lock = Lock()

    def get_odoo_url(self) -> str:
        """Get the appropriate Odoo URL based on environment (dev/prod)

        Returns:
            str: The Odoo server URL
        """
        if not self.connection:
            raise ValueError("No Odoo connection configured")

        # Return the configured URL (stored in odoo_url field)
        return self.connection.odoo_url

    def get_credentials(self) -> Tuple[str, str, str]:
        """Get decrypted Odoo credentials

        Returns:
            tuple: (url, database, api_key)
        """
        if not self.connection:
            raise ValueError("No Odoo connection configured")

        url = self.get_odoo_url()
        database = self.connection.odoo_database
        api_key = self.connection.access_token  # API Key is stored in access_token (encrypted)

        if not all([url, database, api_key]):
            raise ValueError("Odoo credentials incomplete (URL, database or API key missing)")

        return url, database, api_key

    def test_connection(self) -> Tuple[bool, str]:
        """Test connection to Odoo with provided credentials

        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            url, database, api_key = self.get_credentials()

            # Get username from connection (should be stored during setup)
            username = self.connection.company_id_external  # Réutilisation du champ pour stocker username

            if not username:
                return False, "Username Odoo manquant dans la configuration"

            # Connect to Odoo common endpoint
            common = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/common')

            # Test authentication
            uid = common.authenticate(database, username, api_key, {})

            if not uid:
                return False, "Authentification échouée. Vérifiez vos identifiants Odoo."

            # Test access to models
            models = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/object')

            # Try to read res.partner (customers) - just check access
            try:
                models.execute_kw(
                    database, uid, api_key,
                    'res.partner', 'search_read',
                    [[]],
                    {'fields': ['name'], 'limit': 1}
                )
            except Exception as e:
                return False, f"Erreur d'accès aux données: {str(e)}"

            return True, "Connexion réussie"

        except Exception as e:
            logger.error(f"Erreur de connexion Odoo: {str(e)}")
            return False, f"Erreur de connexion: {str(e)}"

    def ensure_valid_credentials(self) -> bool:
        """Ensure credentials are valid

        Note: Odoo API Keys don't expire automatically like OAuth tokens.
        This method just checks if credentials are configured.

        Returns:
            bool: True if credentials are valid
        """
        if not self.connection:
            return False

        try:
            url, database, api_key = self.get_credentials()
            return True
        except ValueError:
            return False

    def _map_customer_fields(self, odoo_partner: Dict, field_mapping: Dict) -> Dict:
        """Map Odoo res.partner fields to internal Client format

        Args:
            odoo_partner: Partner data from Odoo
            field_mapping: Custom field mapping configuration

        Returns:
            dict: Mapped client data
        """
        # Default mapping
        default_mapping = {
            'code_client': 'ref',  # Odoo reference field
            'nom': 'name',
            'email': 'email',
            'telephone': 'phone',
            'adresse': 'street',
            'ville': 'city',
            'code_postal': 'zip',
            'pays': 'country_id'
        }

        # Merge with custom mapping
        mapping = {**default_mapping, **(field_mapping or {})}

        # Extract data
        client_data = {}

        # Code client (required) - use ID if no ref
        odoo_ref = odoo_partner.get(mapping.get('code_client', 'ref'))
        client_data['code_client'] = odoo_ref if odoo_ref else f"ODOO_{odoo_partner['id']}"

        # Name (required)
        client_data['name'] = odoo_partner.get(mapping.get('name', 'name'), '')

        # Optional fields
        client_data['email'] = odoo_partner.get(mapping.get('email', 'email'), '')
        client_data['phone'] = odoo_partner.get(mapping.get('phone', 'phone'), '')

        # Build address from multiple Odoo fields
        address_parts = []
        street = odoo_partner.get(mapping.get('address', 'street'), '')
        if street:
            address_parts.append(street)

        city = odoo_partner.get(mapping.get('city', 'city'), '')
        zip_code = odoo_partner.get(mapping.get('zip', 'zip'), '')
        if city or zip_code:
            city_zip = f"{zip_code} {city}".strip()
            if city_zip:
                address_parts.append(city_zip)

        country = odoo_partner.get(mapping.get('country', 'country_id'))
        if country and isinstance(country, (list, tuple)) and len(country) > 1:
            address_parts.append(country[1])

        client_data['address'] = ', '.join(address_parts) if address_parts else ''

        return client_data

    def _map_invoice_fields(self, odoo_invoice: Dict, field_mapping: Dict, client_id: int) -> Dict:
        """Map Odoo account.move fields to internal Invoice format

        Args:
            odoo_invoice: Invoice data from Odoo (account.move)
            field_mapping: Custom field mapping configuration
            client_id: Internal client ID

        Returns:
            dict: Mapped invoice data
        """
        # Default mapping (using correct Invoice model column names)
        default_mapping = {
            'invoice_number': 'name',
            'invoice_date': 'invoice_date',
            'due_date': 'invoice_date_due',
            'amount': 'amount_residual',  # Use amount_residual (unpaid amount)
            'invoice_id_external': 'id'
        }

        # Merge with custom mapping
        mapping = {**default_mapping, **(field_mapping or {})}

        # Extract data
        invoice_data = {}

        # Required fields
        invoice_data['invoice_number'] = odoo_invoice.get(mapping.get('invoice_number', 'name'), '')
        invoice_data['client_id'] = client_id
        invoice_data['company_id'] = self.connection.company_id

        # Dates
        invoice_date_str = odoo_invoice.get(mapping.get('invoice_date', 'invoice_date'))
        if invoice_date_str:
            if isinstance(invoice_date_str, str):
                invoice_data['invoice_date'] = datetime.strptime(invoice_date_str, '%Y-%m-%d').date()
            else:
                invoice_data['invoice_date'] = invoice_date_str

        due_date_str = odoo_invoice.get(mapping.get('due_date', 'invoice_date_due'))
        if due_date_str:
            if isinstance(due_date_str, str):
                invoice_data['due_date'] = datetime.strptime(due_date_str, '%Y-%m-%d').date()
            else:
                invoice_data['due_date'] = due_date_str

        # Amount (use amount_residual which is unpaid amount)
        invoice_data['amount'] = float(odoo_invoice.get(mapping.get('amount', 'amount_residual'), 0))

        # Original Amount (amount_total is the full invoice amount before payments)
        amount_total = odoo_invoice.get('amount_total')
        invoice_data['original_amount'] = float(amount_total) if amount_total is not None else None

        # All synced invoices are unpaid (filtered by payment_state)
        invoice_data['is_paid'] = False

        # External ID for sync tracking
        invoice_data['invoice_id_external'] = str(odoo_invoice.get('id', ''))

        return invoice_data

    def get_table_headers(self, model_name: str = 'res.partner', sample_size: int = 20) -> Optional[Dict]:
        """Get table headers from Odoo model for preview

        Args:
            model_name: Odoo model name (res.partner, account.move)
            sample_size: Number of sample records to fetch

        Returns:
            dict: Field information with samples
        """
        try:
            url, database, api_key = self.get_credentials()
            username = self.connection.company_id_external

            if not username:
                logger.error("Username Odoo manquant")
                return None

            # Authenticate
            common = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/common')
            uid = common.authenticate(database, username, api_key, {})

            if not uid:
                logger.error("Authentification Odoo échouée")
                return None

            # Get model fields
            models = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/object')

            # Fetch sample data
            records = models.execute_kw(
                database, uid, api_key,
                model_name, 'search_read',
                [[]],
                {'limit': sample_size}
            )

            if not records:
                return {'fields': {}, 'sample_data': []}

            # Build field info from first record
            first_record = records[0]
            fields_info = {}

            for field_name, value in first_record.items():
                field_type = 'text'
                if isinstance(value, bool):
                    field_type = 'boolean'
                elif isinstance(value, (int, float)):
                    field_type = 'number'
                elif isinstance(value, (list, tuple)):
                    field_type = 'relation'

                fields_info[field_name] = {
                    'type': field_type,
                    'sample_value': str(value)[:100] if value else ''
                }

            return {
                'fields': fields_info,
                'sample_data': records[:5]  # Return first 5 for preview
            }

        except Exception as e:
            logger.error(f"Erreur lors de la récupération des champs Odoo: {str(e)}")
            return None

    def download_invoice_pdf(self, invoice_id_external: str) -> bytes:
        """Download invoice PDF from Odoo using portal access token

        Args:
            invoice_id_external: The Odoo invoice ID (account.move ID)

        Returns:
            bytes: The PDF file content

        Raises:
            ValueError: If connection is not available or credentials are invalid
            Exception: If PDF download fails
        """
        if not self.connection:
            raise ValueError("No Odoo connection available")

        if not self.ensure_valid_credentials():
            raise ValueError("Invalid Odoo credentials")

        try:
            import requests

            url, database, api_key = self.get_credentials()
            username = self.connection.company_id_external

            # Authenticate via XML-RPC to get access token
            common = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/common')
            uid = common.authenticate(database, username, api_key, {})

            if not uid:
                raise ValueError("Failed to authenticate with Odoo")

            models = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/object')

            # Get the invoice's portal access token
            invoice_data = models.execute_kw(
                database, uid, api_key,
                'account.move',
                'read',
                [[int(invoice_id_external)]],
                {'fields': ['access_token']}
            )

            if not invoice_data or not invoice_data[0].get('access_token'):
                # If no access token exists, generate one
                models.execute_kw(
                    database, uid, api_key,
                    'account.move',
                    'write',
                    [[int(invoice_id_external)], {'access_token': True}]
                )
                # Read again to get the generated token
                invoice_data = models.execute_kw(
                    database, uid, api_key,
                    'account.move',
                    'read',
                    [[int(invoice_id_external)]],
                    {'fields': ['access_token']}
                )

            access_token = invoice_data[0].get('access_token')
            if not access_token:
                raise Exception("Failed to get or generate access token")

            # Download PDF using the public portal URL (no authentication needed)
            pdf_url = f"{url}/my/invoices/{invoice_id_external}?report_type=pdf&download=true&access_token={access_token}"

            # SSRF protection: validate URL hostname is not private/loopback/link-local
            import ipaddress
            from urllib.parse import urlparse as _urlparse
            _parsed = _urlparse(pdf_url)
            try:
                _ip = ipaddress.ip_address(_parsed.hostname)
                if _ip.is_private or _ip.is_loopback or _ip.is_link_local or _ip.is_reserved:
                    raise Exception(f"SSRF protection: refusing to connect to private/reserved address {_parsed.hostname}")
            except ValueError:
                # Hostname is not an IP literal - resolve it
                import socket
                _resolved = socket.getaddrinfo(_parsed.hostname, None)
                for _family, _type, _proto, _canonname, _sockaddr in _resolved:
                    _resolved_ip = ipaddress.ip_address(_sockaddr[0])
                    if _resolved_ip.is_private or _resolved_ip.is_loopback or _resolved_ip.is_link_local or _resolved_ip.is_reserved:
                        raise Exception(f"SSRF protection: refusing to connect to private/reserved address {_parsed.hostname} ({_sockaddr[0]})")

            response = requests.get(pdf_url, timeout=30)

            if response.status_code != 200:
                raise Exception(f"Failed to download PDF: HTTP {response.status_code}")

            return response.content

        except Exception as e:
            logger.error(f"Error downloading Odoo invoice PDF: {str(e)}")
            raise Exception(f"Failed to download PDF: {str(e)}")

    def sync_customers(self, company_id: int, sync_log_id: Optional[int] = None) -> Tuple[int, int]:
        """Sync customers from Odoo to local database

        Args:
            company_id: ID of the company to sync data for
            sync_log_id: Optional ID of the SyncLog entry for tracking and manual stop

        Returns:
            tuple: (created_count, updated_count)
        """
        logger.info("=== STARTING ODOO CUSTOMER SYNC ===")
        logger.info(f"Starting customer sync for company {company_id}")
        created_count = 0
        updated_count = 0
        manual_stop_requested = False
        sync_log = None

        try:
            from app import db
            from models import Client, Company, CompanySyncUsage, SyncLog

            # Load SyncLog if provided for manual stop support
            if sync_log_id:
                sync_log = SyncLog.query.get(sync_log_id)

            # SÉCURITÉ CRITIQUE: Verify connection integrity
            if not self.connection:
                raise ValueError("No Odoo connection configured")

            if self.connection.company_id != company_id:
                raise ValueError(
                    f"SÉCURITÉ: Cross-company sync attempt detected. "
                    f"Connection belongs to company {self.connection.company_id} but sync requested for company {company_id}"
                )

            # Check sync limits before starting
            if not CompanySyncUsage.check_company_sync_limit(company_id):
                raise Exception("Daily sync limit reached for your plan.")

            # Get credentials
            url, database, api_key = self.get_credentials()
            username = self.connection.company_id_external

            if not username:
                raise ValueError("Odoo username not configured")

            # Authenticate
            common = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/common')
            uid = common.authenticate(database, username, api_key, {})

            if not uid:
                raise ValueError("Odoo authentication failed")

            # Fetch ALL customers from Odoo with pagination
            models = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/object')
            all_customers = []
            offset = 0
            limit = 100  # Batch size

            while True:
                # Check for manual stop request
                if sync_log:
                    db.session.refresh(sync_log)
                    if sync_log.is_stop_requested():
                        manual_stop_requested = True
                        logging.warning(f"🛑 Manual stop requested for Odoo sync_customers (SyncLog {sync_log_id})")
                        break

                # Fetch batch of customers (only customers, not suppliers)
                # Odoo 13+: Use customer_rank instead of customer field
                search_options = {
                    'fields': ['id', 'ref', 'name', 'email', 'phone', 'street', 'city', 'zip', 'country_id'],
                    'limit': limit,
                    'offset': offset
                }
                customers_batch = models.execute_kw(
                    database, uid, api_key,
                    'res.partner', 'search_read',
                    [[['customer_rank', '>', 0]]],
                    search_options
                )

                if not customers_batch:
                    break

                all_customers.extend(customers_batch)

                if len(customers_batch) < limit:
                    break

                offset += limit

            logger.info(f"Total customers retrieved from Odoo: {len(all_customers)}")

            # Get field mapping
            field_mapping = self.connection.get_field_mapping()

            # SÉCURITÉ LICENCE: Count new clients before creation
            company = Company.query.get(company_id)
            if not company:
                raise ValueError(f"Company {company_id} not found")

            new_clients_count = 0
            for odoo_customer in all_customers:
                client_data = self._map_customer_fields(odoo_customer, field_mapping)
                # Check if client already exists
                existing_client = Client.query.filter_by(
                    code_client=client_data['code_client'],
                    company_id=company_id
                ).first()
                if not existing_client:
                    new_clients_count += 1

            # VÉRIFICATION CRUCIALE: Respect plan limits
            if new_clients_count > 0:
                try:
                    company.assert_client_capacity(new_clients_count)
                except ValueError as e:
                    raise Exception(f"🚫 Odoo import blocked: {str(e)}")

            # Track synced customer codes
            odoo_customer_codes = []

            # Process customers
            for odoo_customer in all_customers:
                # Check for manual stop
                if manual_stop_requested:
                    break

                try:
                    client_data = self._map_customer_fields(odoo_customer, field_mapping)
                    odoo_customer_codes.append(client_data['code_client'])

                    # Check if client already exists
                    existing_client = Client.query.filter_by(
                        code_client=client_data['code_client'],
                        company_id=company_id
                    ).first()

                    if existing_client:
                        # Update existing client
                        for key, value in client_data.items():
                            if key not in ['code_client', 'company_id']:
                                setattr(existing_client, key, value)
                        updated_count += 1
                    else:
                        # Create new client
                        client_data['company_id'] = company_id
                        new_client = Client(**client_data)
                        db.session.add(new_client)
                        created_count += 1

                    # Update sync log progress
                    if sync_log and (created_count + updated_count) % 10 == 0:
                        sync_log.clients_synced = created_count + updated_count
                        sync_log.last_activity_at = datetime.utcnow()
                        db.session.commit()

                except Exception as e:
                    logger.error(f"Error processing Odoo customer {odoo_customer.get('id')}: {str(e)}")
                    continue

            # Save changes
            db.session.commit()

            # Update sync log
            if sync_log:
                sync_log.clients_synced = created_count + updated_count
                sync_log.last_activity_at = datetime.utcnow()

                if manual_stop_requested:
                    sync_log.status = 'stopped'
                    sync_log.completed_at = datetime.utcnow()
                    sync_log.error_message = f"Stopped manually after {created_count} creations, {updated_count} updates"
                    logging.warning(f"🛑 Odoo sync_customers stopped manually after {created_count} creations, {updated_count} updates")
                else:
                    sync_log.status = 'completed'
                    sync_log.completed_at = datetime.utcnow()

                db.session.commit()

            logger.info(f"=== ODOO CUSTOMER SYNC COMPLETE: {created_count} created, {updated_count} updated ===")
            return created_count, updated_count

        except Exception as e:
            logger.error(f"Error during Odoo customer sync: {str(e)}")

            if sync_log:
                from app import db
                sync_log.status = 'failed'
                sync_log.completed_at = datetime.utcnow()
                sync_log.error_message = str(e)
                db.session.commit()

            raise

    def sync_invoices(self, company_id: int, sync_log_id: Optional[int] = None) -> Tuple[int, int]:
        """Sync invoices from Odoo to local database

        Args:
            company_id: ID of the company to sync data for
            sync_log_id: Optional ID of the SyncLog entry for tracking and manual stop

        Returns:
            tuple: (created_count, updated_count)
        """
        logger.info("=== STARTING ODOO INVOICE SYNC ===")
        logger.info(f"Starting invoice sync for company {company_id}")
        created_count = 0
        updated_count = 0
        manual_stop_requested = False
        sync_log = None

        try:
            from app import db
            from models import Client, Invoice, CompanySyncUsage, SyncLog

            # Load SyncLog if provided
            if sync_log_id:
                sync_log = SyncLog.query.get(sync_log_id)

            # SÉCURITÉ CRITIQUE: Verify connection integrity
            if not self.connection:
                raise ValueError("No Odoo connection configured")

            if self.connection.company_id != company_id:
                raise ValueError(
                    f"SÉCURITÉ: Cross-company sync attempt detected. "
                    f"Connection belongs to company {self.connection.company_id} but sync requested for company {company_id}"
                )

            # Check sync limits
            if not CompanySyncUsage.check_company_sync_limit(company_id):
                raise Exception("Daily sync limit reached for your plan.")

            # Get credentials
            url, database, api_key = self.get_credentials()
            username = self.connection.company_id_external

            if not username:
                raise ValueError("Odoo username not configured")

            # Authenticate
            common = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/common')
            uid = common.authenticate(database, username, api_key, {})

            if not uid:
                raise ValueError("Odoo authentication failed")

            # Fetch unpaid invoices from Odoo with pagination
            models = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/object')
            all_invoices = []
            offset = 0
            limit = 100

            while True:
                # Check for manual stop
                if sync_log:
                    db.session.refresh(sync_log)
                    if sync_log.is_stop_requested():
                        manual_stop_requested = True
                        logging.warning(f"🛑 Manual stop requested for Odoo sync_invoices (SyncLog {sync_log_id})")
                        break

                # Fetch batch of invoices (only posted invoices with unpaid balance)
                invoices_batch = models.execute_kw(
                    database, uid, api_key,
                    'account.move', 'search_read',
                    [[
                        ['move_type', '=', 'out_invoice'],
                        ['state', '=', 'posted'],
                        ['payment_state', 'in', ['not_paid', 'partial']]
                    ]],
                    {
                        'fields': ['id', 'name', 'partner_id', 'invoice_date', 'invoice_date_due',
                                   'amount_total', 'amount_residual', 'payment_state'],
                        'limit': limit,
                        'offset': offset
                    }
                )

                if not invoices_batch:
                    break

                all_invoices.extend(invoices_batch)

                if len(invoices_batch) < limit:
                    break

                offset += limit

            logger.info(f"Total invoices retrieved from Odoo: {len(all_invoices)}")

            # Get field mapping
            field_mapping = self.connection.get_field_mapping()

            # Load all clients into memory for fast lookup (like Business Central)
            logger.info("Loading existing clients for fast lookup...")
            clients_by_code = {}
            clients_by_odoo_id = {}

            for client in Client.query.filter_by(company_id=company_id).all():
                clients_by_code[client.code_client] = client
                # Extract Odoo ID from code_client if it follows ODOO_{id} pattern
                if client.code_client.startswith('ODOO_'):
                    odoo_id = client.code_client.replace('ODOO_', '')
                    clients_by_odoo_id[odoo_id] = client

            logger.info(f"Loaded {len(clients_by_code)} clients for lookup")

            # Process invoices
            for odoo_invoice in all_invoices:
                # Check for manual stop
                if manual_stop_requested:
                    break

                try:
                    # Get partner (customer) ID from invoice
                    partner_id = odoo_invoice.get('partner_id')
                    if not partner_id or not isinstance(partner_id, (list, tuple)):
                        logger.warning(f"Invoice {odoo_invoice.get('name')} has no valid partner")
                        continue

                    partner_odoo_id = partner_id[0] if isinstance(partner_id, (list, tuple)) else partner_id

                    # Find matching client using in-memory lookup
                    client = clients_by_odoo_id.get(str(partner_odoo_id))

                    if not client:
                        logger.warning(f"Client not found for Odoo partner {partner_odoo_id}, skipping invoice")
                        continue

                    # Map invoice data
                    invoice_data = self._map_invoice_fields(odoo_invoice, field_mapping, client.id)

                    # Check if invoice already exists
                    existing_invoice = Invoice.query.filter_by(
                        invoice_number=invoice_data['invoice_number'],
                        client_id=client.id
                    ).first()

                    if existing_invoice:
                        # Update existing invoice
                        for key, value in invoice_data.items():
                            if key not in ['invoice_number', 'client_id', 'company_id']:
                                setattr(existing_invoice, key, value)
                        updated_count += 1
                    else:
                        # Create new invoice
                        new_invoice = Invoice(**invoice_data)
                        db.session.add(new_invoice)
                        created_count += 1

                    # Update sync log progress
                    if sync_log and (created_count + updated_count) % 10 == 0:
                        sync_log.invoices_synced = created_count + updated_count
                        sync_log.last_activity_at = datetime.utcnow()
                        db.session.commit()

                except Exception as e:
                    logger.error(f"Error processing Odoo invoice {odoo_invoice.get('name')}: {str(e)}")
                    continue

            # Save changes
            db.session.commit()

            # Delete invoices that are no longer in the unpaid list from Odoo (they have been paid)
            deleted_count = 0
            if not manual_stop_requested:
                # Get all invoice_id_external from Odoo response
                odoo_unpaid_external_ids = set(str(inv.get('id', '')) for inv in all_invoices)

                # Find invoices in our DB that have an external ID but are not in the unpaid list
                existing_unpaid_invoices = Invoice.query.filter(
                    Invoice.company_id == company_id,
                    Invoice.is_paid == False,
                    Invoice.invoice_id_external.isnot(None),
                    Invoice.invoice_id_external != ''
                ).all()

                for invoice in existing_unpaid_invoices:
                    if invoice.invoice_id_external not in odoo_unpaid_external_ids:
                        # This invoice is no longer in the unpaid list - delete it (it's been paid)
                        db.session.delete(invoice)
                        deleted_count += 1

                if deleted_count > 0:
                    db.session.commit()
                    logger.info(f"Deleted {deleted_count} paid invoices")

            # Update sync log
            if sync_log:
                sync_log.invoices_synced = created_count + updated_count
                sync_log.last_activity_at = datetime.utcnow()

                if manual_stop_requested:
                    sync_log.status = 'stopped'
                    sync_log.completed_at = datetime.utcnow()
                    sync_log.error_message = f"Stopped manually after {created_count} creations, {updated_count} updates"
                    logging.warning(f"🛑 Odoo sync_invoices stopped manually")
                else:
                    sync_log.status = 'completed'
                    sync_log.completed_at = datetime.utcnow()

                db.session.commit()

            logger.info(f"=== ODOO INVOICE SYNC COMPLETE: {created_count} created, {updated_count} updated, {deleted_count} deleted ===")

            # Enregistrer snapshot des CAR (non bloquant) - seulement si sync completed (pas stopped)
            if not manual_stop_requested:
                try:
                    from utils.receivables_snapshot import create_receivables_snapshot
                    create_receivables_snapshot(company_id, trigger_type='sync')
                except Exception as snapshot_error:
                    logger.warning(f"Snapshot CAR non créé: {snapshot_error}")

            return created_count, updated_count

        except Exception as e:
            logger.error(f"Error during Odoo invoice sync: {str(e)}")

            if sync_log:
                from app import db
                sync_log.status = 'failed'
                sync_log.completed_at = datetime.utcnow()
                sync_log.error_message = str(e)
                db.session.commit()

            raise

    def sync_payments(self, company_id: int, sync_log_id: Optional[int] = None) -> int:
        """Synchronise les paiements reçus depuis Odoo vers la table ReceivedPayment.

        Stratégie : interroger TOUTES les account.move payées chez Odoo (sans filtre
        sur les IDs locaux) pour couvrir l'historique complet — y compris les factures
        déjà supprimées localement (payées lors d'une sync précédente).
        Le mapping client se fait via code_client = 'ODOO_{partner_id}'.
        Les métadonnées de la facture (dates, montant, numéro) viennent directement
        d'Odoo — aucune dépendance sur la table Invoice locale.

        Args:
            company_id: ID de l'entreprise à synchroniser
            sync_log_id: ID optionnel du SyncLog pour le suivi

        Returns:
            int: Nombre d'enregistrements ReceivedPayment créés
        """
        logger.info("=== STARTING ODOO PAYMENT SYNC ===")
        created_count = 0

        try:
            from app import db
            from models import Client, ReceivedPayment

            if not self.connection:
                raise ValueError("No Odoo connection configured")

            if self.connection.company_id != company_id:
                raise ValueError(
                    f"SÉCURITÉ: Cross-company sync attempt. "
                    f"Connection belongs to company {self.connection.company_id} "
                    f"but sync requested for company {company_id}"
                )

            url, database, api_key = self.get_credentials()
            username = self.connection.company_id_external

            if not username:
                raise ValueError("Odoo username not configured")

            common = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/common')
            uid = common.authenticate(database, username, api_key, {})

            if not uid:
                raise ValueError("Odoo authentication failed")

            models_proxy = xmlrpc_client.ServerProxy(f'{url}/xmlrpc/2/object')

            clients_by_odoo_id = {}
            for client in Client.query.filter_by(company_id=company_id).all():
                if client.code_client and client.code_client.startswith('ODOO_'):
                    odoo_partner_id = client.code_client.replace('ODOO_', '')
                    clients_by_odoo_id[odoo_partner_id] = client

            logger.info(
                f"Loaded {len(clients_by_odoo_id)} clients for Odoo partner mapping"
            )

            existing_keys = set(
                f"{rp.external_payment_id}|{rp.invoice_number}"
                for rp in ReceivedPayment.query.filter_by(
                    company_id=company_id,
                    source='odoo'
                ).with_entities(
                    ReceivedPayment.external_payment_id,
                    ReceivedPayment.invoice_number
                ).all()
            )

            # Sync incrémentale : ne tirer que les factures modifiées depuis
            # la dernière sync. On ancre sur le max(created_at) de nos
            # enregistrements existants, avec -1 jour de sécurité (décalages
            # horaires, latence Odoo). Première sync = full historique.
            last_sync_row = (
                db.session.query(db.func.max(ReceivedPayment.created_at))
                .filter_by(company_id=company_id, source='odoo')
                .scalar()
            )

            domain_base = [
                ['move_type', '=', 'out_invoice'],
                ['state', '=', 'posted'],
                ['payment_state', 'in', ['paid', 'in_payment']]
            ]

            if last_sync_row:
                from datetime import timedelta
                cutoff = (last_sync_row - timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
                domain_base.append(['write_date', '>=', cutoff])
                logger.info(
                    f"Incremental sync: fetching moves with write_date >= {cutoff}"
                )
            else:
                logger.info("Full historical sync: no previous records found")

            offset = 0
            batch_size = 500

            while True:
                odoo_moves = models_proxy.execute_kw(
                    database, uid, api_key,
                    'account.move', 'search_read',
                    [domain_base],
                    {
                        'fields': [
                            'id', 'name', 'partner_id',
                            'invoice_date', 'invoice_date_due',
                            'amount_total', 'invoice_payments_widget'
                        ],
                        'limit': batch_size,
                        'offset': offset
                    }
                )

                if not odoo_moves:
                    break

                logger.info(
                    f"Batch offset={offset}: {len(odoo_moves)} paid invoices "
                    f"returned from Odoo"
                )

                for move in odoo_moves:
                    partner_id = move.get('partner_id')
                    if not partner_id or not isinstance(partner_id, (list, tuple)):
                        continue

                    odoo_partner_id_str = str(partner_id[0])
                    client = clients_by_odoo_id.get(odoo_partner_id_str)
                    if not client:
                        continue

                    invoice_number = move.get('name') or ''
                    if not invoice_number:
                        continue

                    invoice_date_raw = move.get('invoice_date')
                    invoice_date = None
                    if invoice_date_raw and invoice_date_raw is not False:
                        try:
                            invoice_date = datetime.strptime(
                                invoice_date_raw, '%Y-%m-%d'
                            ).date()
                        except (ValueError, TypeError):
                            pass

                    invoice_due_date_raw = move.get('invoice_date_due')
                    invoice_due_date = None
                    if invoice_due_date_raw and invoice_due_date_raw is not False:
                        try:
                            invoice_due_date = datetime.strptime(
                                invoice_due_date_raw, '%Y-%m-%d'
                            ).date()
                        except (ValueError, TypeError):
                            pass

                    amount_total = float(move.get('amount_total') or 0)
                    odoo_move_id_str = str(move['id'])

                    widget_raw = move.get('invoice_payments_widget')
                    if not widget_raw or widget_raw is False:
                        continue

                    try:
                        if isinstance(widget_raw, str):
                            widget = json.loads(widget_raw)
                        else:
                            widget = widget_raw
                        payment_entries = widget.get('content') or []
                    except (ValueError, TypeError, AttributeError):
                        logger.warning(
                            f"Cannot parse invoice_payments_widget for move {move['id']}"
                        )
                        continue

                    for entry in payment_entries:
                        payment_move_id = entry.get('move_id')
                        if isinstance(payment_move_id, (list, tuple)):
                            payment_move_id = payment_move_id[0]
                        external_payment_id = str(payment_move_id) if payment_move_id else None
                        if not external_payment_id:
                            continue

                        dedup_key = f"{external_payment_id}|{invoice_number}"
                        if dedup_key in existing_keys:
                            continue

                        payment_date_raw = entry.get('date')
                        if not payment_date_raw:
                            continue
                        try:
                            payment_date = datetime.strptime(
                                payment_date_raw, '%Y-%m-%d'
                            ).date()
                        except (ValueError, TypeError):
                            continue

                        payment_amount = float(entry.get('amount', 0) or 0)

                        received_payment = ReceivedPayment(
                            company_id=company_id,
                            client_id=client.id,
                            invoice_number=invoice_number,
                            invoice_date=invoice_date,
                            invoice_due_date=invoice_due_date,
                            original_invoice_amount=amount_total,
                            payment_date=payment_date,
                            payment_amount=payment_amount,
                            source='odoo',
                            external_payment_id=external_payment_id,
                            external_invoice_id=odoo_move_id_str
                        )
                        db.session.add(received_payment)
                        existing_keys.add(dedup_key)
                        created_count += 1

                try:
                    db.session.commit()
                except Exception as commit_error:
                    logger.warning(
                        f"Batch commit error in sync_payments (skipping batch): {commit_error}"
                    )
                    db.session.rollback()

                if len(odoo_moves) < batch_size:
                    break

                offset += batch_size

            logger.info(
                f"=== ODOO PAYMENT SYNC COMPLETE: {created_count} records created ==="
            )
            return created_count

        except Exception as e:
            logger.error(f"Error during Odoo payment sync: {str(e)}")
            try:
                db.session.rollback()
            except Exception:
                pass
            return created_count


def get_odoo_connector(company_id: int):
    """Get Odoo connector for a company

    Args:
        company_id: Company ID

    Returns:
        OdooConnector or None
    """
    from models import AccountingConnection

    connection = AccountingConnection.query.filter_by(
        company_id=company_id,
        system_type='odoo',
        is_active=True
    ).first()

    if connection:
        return OdooConnector(connection.id, company_id)

    return None
