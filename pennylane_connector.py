"""
Pennylane Accounting Connector
Handles OAuth 2.0 authentication and data synchronization with Pennylane API v2

API Documentation: https://pennylane.readme.io

SECURITE ET STANDARDS:
- OAuth 2.0 avec gestion des tokens rotatifs (refresh token change a chaque refresh)
- RobustHTTPSession avec retry automatique
- Encryption AES-256 des tokens
- Company isolation stricte (protection IDOR)
- Verification limites de plan avant import
- Rate limiting client-side (25 req / 5 sec)
- Read-only: scopes customers:readonly, customer_invoices:readonly
"""

import json
import logging
import requests
import time
from datetime import datetime, timedelta, date
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode
from flask import current_app, url_for

logger = logging.getLogger(__name__)


class PennylaneConnector:
    """Pennylane Accounting API v2 connector with OAuth 2.0"""

    # Pennylane OAuth 2.0 endpoints
    AUTHORIZATION_URL = 'https://app.pennylane.com/oauth/authorize'
    TOKEN_URL = 'https://app.pennylane.com/oauth/token'
    API_BASE_URL = 'https://app.pennylane.com/api/external/v2'

    # Required scopes (read-only)
    SCOPES = 'customers:readonly customer_invoices:readonly'

    # Pagination
    PAGE_LIMIT = 100  # Max items per page for standard endpoints
    CHANGELOG_LIMIT = 1000  # Max items per page for changelog endpoints

    # Rate limiting
    RATE_LIMIT_THRESHOLD = 3  # Sleep proactively when remaining requests <= this

    # Statuses to keep for invoices (unpaid, actionable)
    VALID_INVOICE_STATUSES = {'upcoming', 'late', 'partially_paid', 'incomplete'}

    # Statuses to ignore (paid, cancelled, drafts, estimates, etc.)
    IGNORED_INVOICE_STATUSES = {
        'draft', 'cancelled', 'credit_note', 'proforma', 'shipping_order',
        'purchasing_order', 'estimate_pending', 'estimate_accepted',
        'estimate_invoiced', 'estimate_denied', 'archived', 'paid',
        'partially_cancelled'
    }

    # Payment conditions mapping (Pennylane -> display)
    PAYMENT_CONDITIONS_MAP = {
        'upon_receipt': 'Upon Receipt',
        'custom': 'Custom',
        '7_days': 'Net 7',
        '15_days': 'Net 15',
        '30_days': 'Net 30',
        '30_days_end_of_month': 'Net 30 EOM',
        '45_days': 'Net 45',
        '45_days_end_of_month': 'Net 45 EOM',
        '60_days': 'Net 60',
    }

    def __init__(self, connection_id: Optional[int] = None, company_id: Optional[int] = None):
        """Initialize connector with optional existing connection

        Args:
            connection_id: ID of the Pennylane connection
            company_id: ID of the company that owns the connection (for security validation)
        """
        import os

        self.connection = None
        if connection_id:
            from models import AccountingConnection
            self.connection = AccountingConnection.query.get(connection_id)

            # SECURITE CRITIQUE: Verifier que la connexion appartient a l'entreprise specifiee
            if self.connection and company_id and self.connection.company_id != company_id:
                raise ValueError(
                    f"SECURITE: Tentative d'acces a une connexion Pennylane non autorisee. "
                    f"Connection {connection_id} n'appartient pas a l'entreprise {company_id}"
                )

            if self.connection and not company_id:
                logging.warning(
                    f"SECURITE: Connexion Pennylane {connection_id} chargee sans validation de l'entreprise"
                )

        # Load credentials from environment variables
        self.client_id = os.environ.get('PENNYLANE_CLIENT_ID')
        self.client_secret = os.environ.get('PENNYLANE_CLIENT_SECRET')

        # Cached HTTP session for connection pooling (created lazily)
        self._session = None

        # Cached customer index (Pennylane ID -> local client ID)
        self._customer_index = None

    def get_authorization_url(self, state: str) -> str:
        """Generate OAuth authorization URL for Pennylane"""
        if not self.client_id:
            raise ValueError("Pennylane Client ID not configured")

        redirect_uri = url_for('company.pennylane_callback', _external=True)

        params = {
            'response_type': 'code',
            'client_id': self.client_id,
            'redirect_uri': redirect_uri,
            'scope': self.SCOPES,
            'state': state
        }

        return f"{self.AUTHORIZATION_URL}?{urlencode(params)}"

    def exchange_code_for_tokens(self, authorization_code: str, state: str) -> Dict:
        """Exchange authorization code for access and refresh tokens

        Returns dict with: access_token, refresh_token, expires_in, account_id
        """
        if not self.client_id or not self.client_secret:
            raise ValueError("Pennylane credentials not configured")

        redirect_uri = url_for('company.pennylane_callback', _external=True)

        data = {
            'grant_type': 'authorization_code',
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'code': authorization_code,
            'redirect_uri': redirect_uri
        }

        try:
            response = requests.post(self.TOKEN_URL, data=data, timeout=30)
            response.raise_for_status()
            token_data = response.json()

            access_token = token_data.get('access_token')
            refresh_token = token_data.get('refresh_token')

            if not access_token or not refresh_token:
                raise ValueError(
                    f"Pennylane token response missing required fields. "
                    f"Keys received: {list(token_data.keys())}"
                )

            # Get account info to store as company_id_external
            account_id = self._get_account_id(access_token)

            return {
                'access_token': access_token,
                'refresh_token': refresh_token,
                'expires_in': token_data.get('expires_in', 86400),  # 24 hours
                'account_id': account_id or 'pennylane'
            }

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to exchange Pennylane authorization code: {e}")
            raise ValueError(f"Failed to obtain Pennylane tokens: {str(e)}")

    def _get_account_id(self, access_token: str) -> Optional[str]:
        """Get Pennylane account identifier using the /me endpoint"""
        try:
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json'
            }

            response = requests.get(f"{self.API_BASE_URL}/me", headers=headers, timeout=30)
            response.raise_for_status()

            me_data = response.json()
            # Return email or id as identifier
            return str(me_data.get('id', me_data.get('email', '')))

        except Exception as e:
            logger.error(f"Failed to get Pennylane account info: {e}")
            return None

    def refresh_access_token(self) -> bool:
        """Refresh the access token using refresh token

        IMPORTANT: Pennylane uses ROTATING refresh tokens - the old refresh token
        becomes invalid after use, and a new one is returned.
        Refresh token lifetime: 90 days.
        """
        if not self.connection or not self.connection.refresh_token:
            return False

        if not self.client_id or not self.client_secret:
            raise ValueError("Pennylane credentials not configured")

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

            new_access_token = token_data.get('access_token')
            if not new_access_token:
                logger.error(f"Pennylane refresh response missing access_token. Keys: {list(token_data.keys())}")
                return False

            # Update connection with new tokens
            self.connection.access_token = new_access_token

            # CRITICAL: Update refresh token (Pennylane uses rotating tokens)
            if 'refresh_token' in token_data:
                self.connection.refresh_token = token_data['refresh_token']

            self.connection.token_expires_at = datetime.utcnow() + timedelta(
                seconds=token_data.get('expires_in', 86400)
            )

            from app import db
            db.session.commit()

            logger.info(f"Successfully refreshed Pennylane access token for connection {self.connection.id}")
            return True

        except Exception as e:
            logger.error(f"Failed to refresh Pennylane token: {e}")
            return False

    def _get_session(self):
        """Get or create the cached HTTP session"""
        if self._session is None:
            from utils.http_client import create_pennylane_session
            self._session = create_pennylane_session()
        return self._session

    def make_api_request(self, endpoint: str, method: str = 'GET', params: Dict = None,
                         max_retries: int = 3) -> requests.Response:
        """Make authenticated API request to Pennylane

        Args:
            endpoint: API endpoint (e.g., 'customers', 'customer_invoices')
            method: HTTP method (GET only for read-only connector)
            params: Query parameters
            max_retries: Max retries on 429 rate limit

        Returns:
            requests.Response object (caller handles JSON parsing)
        """
        if not self.connection:
            raise ValueError("No Pennylane connection available")

        # Check if token needs refreshing
        if not self.connection.is_token_valid():
            if not self.refresh_access_token():
                raise ValueError("Failed to refresh Pennylane access token")

        url = f"{self.API_BASE_URL}/{endpoint}"

        headers = {
            'Authorization': f'Bearer {self.connection.access_token}',
            'Accept': 'application/json'
        }

        session = self._get_session()

        for attempt in range(max_retries):
            response = session.request(method, url, headers=headers, params=params)

            # Handle rate limiting with retry loop
            if response.status_code == 429:
                retry_after = int(response.headers.get('retry-after', 5))
                logger.warning(f"Pennylane rate limit hit (attempt {attempt + 1}/{max_retries}), sleeping {retry_after}s")
                time.sleep(retry_after)
                continue

            # Proactive rate limit avoidance on success
            self._handle_rate_limit(response)

            response.raise_for_status()
            return response

        # All retries exhausted
        raise requests.exceptions.HTTPError(
            f"Pennylane rate limit exceeded after {max_retries} retries", response=response
        )

    def _handle_rate_limit(self, response: requests.Response) -> None:
        """Proactive rate limit avoidance for Pennylane (25 req / 5 sec)

        Reads ratelimit-* headers and sleeps proactively when approaching the limit.
        429 responses are handled by the retry loop in make_api_request().
        """
        remaining = response.headers.get('ratelimit-remaining')
        if remaining is not None:
            remaining = int(remaining)
            if remaining <= self.RATE_LIMIT_THRESHOLD:
                reset_ts = response.headers.get('ratelimit-reset')
                if reset_ts:
                    sleep_time = max(0, int(reset_ts) - int(time.time()))
                    if 0 < sleep_time <= 10:
                        logger.debug(f"Pennylane rate limit approaching ({remaining} remaining), sleeping {sleep_time}s")
                        time.sleep(sleep_time)

    def _paginate_cursor(self, endpoint: str, params: Dict = None, limit: int = None) -> List[Dict]:
        """Paginate through a Pennylane endpoint using cursor-based pagination

        Args:
            endpoint: API endpoint
            params: Additional query parameters (filters, sort, etc.)
            limit: Items per page (default: PAGE_LIMIT)

        Yields batches of items from each page.
        Returns all items collected.
        """
        if params is None:
            params = {}

        if limit is None:
            limit = self.PAGE_LIMIT

        params['limit'] = limit
        all_items = []
        cursor = None

        while True:
            page_params = dict(params)
            if cursor:
                page_params['cursor'] = cursor

            response = self.make_api_request(endpoint, params=page_params)
            data = response.json()

            items = data.get('items', [])
            all_items.extend(items)

            if not data.get('has_more', False) or not data.get('next_cursor'):
                break

            cursor = data['next_cursor']

        return all_items

    def test_connection(self) -> Tuple[bool, str]:
        """Test the Pennylane connection"""
        try:
            response = self.make_api_request('me')
            me_data = response.json()

            email = me_data.get('email', 'Pennylane')
            return True, f"Connexion reussie ({email})"

        except Exception as e:
            return False, f"Erreur de connexion: {str(e)}"

    # =========================================================================
    # SYNC CUSTOMERS
    # =========================================================================

    def sync_customers(self, company_id: int, sync_log_id: Optional[int] = None) -> Tuple[int, int]:
        """Sync customers from Pennylane to local database

        Args:
            company_id: ID of the company to sync data for
            sync_log_id: Optional ID of the SyncLog entry for tracking

        Returns:
            Tuple of (created_count, updated_count)
        """
        created_count = 0
        updated_count = 0
        sync_log = None

        try:
            from app import db
            from models import Client, CompanySyncUsage, SyncLog, Company

            # Load SyncLog if provided
            if sync_log_id:
                sync_log = SyncLog.query.get(sync_log_id)
                if sync_log:
                    sync_log.status = 'running'
                    db.session.commit()

            # SECURITE CRITIQUE: Verifier l'integrite des donnees
            if not self.connection:
                raise ValueError("Aucune connexion Pennylane configuree")

            if self.connection.company_id != company_id:
                raise ValueError(
                    f"SECURITE: Tentative de synchronisation croisee detectee. "
                    f"Connexion appartient a l'entreprise {self.connection.company_id} "
                    f"mais tentative de sync pour l'entreprise {company_id}"
                )

            # Verifier les limites de synchronisation
            if not CompanySyncUsage.check_company_sync_limit(company_id):
                raise Exception("Limite quotidienne de synchronisation atteinte pour votre forfait.")

            # Fetch ALL customers from Pennylane with cursor pagination
            all_customers = []
            cursor = None

            while True:
                # Check for manual stop
                if sync_log:
                    db.session.refresh(sync_log)
                    if sync_log.is_stop_requested():
                        logger.warning(f"Arret manuel demande pour Pennylane sync_customers (SyncLog {sync_log_id})")
                        break

                params = {'limit': self.PAGE_LIMIT}
                if cursor:
                    params['cursor'] = cursor

                response = self.make_api_request('customers', params=params)
                data = response.json()

                batch = data.get('items', [])
                if not batch:
                    break

                all_customers.extend(batch)

                if not data.get('has_more', False) or not data.get('next_cursor'):
                    break

                cursor = data['next_cursor']

            logger.info(f"Total clients recuperes de Pennylane: {len(all_customers)}")

            # SECURITE LICENCE: Compter les nouveaux clients avant creation
            company = Company.query.get(company_id)
            if not company:
                raise ValueError(f"Entreprise {company_id} non trouvee")

            # Pre-load all existing clients into a dict for O(1) lookups (N+1 fix)
            existing_clients_by_code = {
                c.code_client: c for c in Client.query.filter_by(company_id=company_id).all()
            }

            new_clients_count = 0
            for pl_customer in all_customers:
                client_data = self._map_customer_fields(pl_customer)
                if client_data['code_client'] not in existing_clients_by_code:
                    new_clients_count += 1

            # VERIFICATION CRUCIALE: Respect des limites de plan
            if new_clients_count > 0:
                try:
                    company.assert_client_capacity(new_clients_count)
                    logger.info(
                        f"Pennylane sync autorise: {new_clients_count} nouveaux clients "
                        f"(plan: {company.get_plan_display_name()})"
                    )
                except ValueError as e:
                    raise Exception(f"Import Pennylane bloque: {str(e)}")

            # Create/update clients
            for pl_customer in all_customers:
                client_data = self._map_customer_fields(pl_customer)
                code = client_data['code_client']

                existing_client = existing_clients_by_code.get(code)

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
                    existing_clients_by_code[code] = new_client
                    created_count += 1

            # NOTE: Quota increment is done once in the route handler (pennylane_sync),
            # not here, to avoid triple-counting.

            db.session.commit()

        except Exception as e:
            logger.error(f"Error syncing Pennylane customers: {e}")
            from app import db
            db.session.rollback()
            raise

        return created_count, updated_count

    # =========================================================================
    # SYNC INVOICES
    # =========================================================================

    def sync_invoices(self, company_id: int, sync_log_id: Optional[int] = None) -> Tuple[int, int]:
        """Sync invoices from Pennylane to local database

        Only syncs unpaid invoices (paid == false, remaining_amount_with_tax > 0).
        Deletes locally paid invoices (clean slate approach).

        Args:
            company_id: ID of the company to sync data for
            sync_log_id: Optional ID of the SyncLog entry for tracking

        Returns:
            Tuple of (created_count, updated_count)
        """
        created_count = 0
        updated_count = 0
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

            # SECURITE CRITIQUE
            if not self.connection:
                raise ValueError("Aucune connexion Pennylane configuree")

            if self.connection.company_id != company_id:
                raise ValueError(
                    f"SECURITE: Tentative de synchronisation croisee detectee. "
                    f"Connexion appartient a l'entreprise {self.connection.company_id} "
                    f"mais tentative de sync pour l'entreprise {company_id}"
                )

            # Verifier les limites de synchronisation
            if not CompanySyncUsage.check_company_sync_limit(company_id):
                raise Exception("Limite quotidienne de synchronisation atteinte pour votre forfait.")

            # Build Pennylane customer_id -> local client_id index (cached on instance)
            customer_id_to_client = self._get_customer_index(company_id)

            # Fetch ALL non-draft invoices from Pennylane with cursor pagination
            all_invoices = []
            cursor = None

            # Filter: exclude drafts (Pennylane uses 'filters' plural, field 'status', string values)
            filter_param = json.dumps([{"field": "status", "operator": "not_eq", "value": "draft"}])

            while True:
                # Check for manual stop
                if sync_log:
                    db.session.refresh(sync_log)
                    if sync_log.is_stop_requested():
                        logger.warning(f"Arret manuel demande pour Pennylane sync_invoices (SyncLog {sync_log_id})")
                        break

                params = {
                    'limit': self.PAGE_LIMIT,
                    'filters': filter_param
                }
                if cursor:
                    params['cursor'] = cursor

                response = self.make_api_request('customer_invoices', params=params)
                data = response.json()

                batch = data.get('items', [])
                if not batch:
                    break

                all_invoices.extend(batch)

                if not data.get('has_more', False) or not data.get('next_cursor'):
                    break

                cursor = data['next_cursor']

            logger.info(f"Total factures recuperees de Pennylane: {len(all_invoices)}")

            # Pre-load all existing invoices into a dict for O(1) lookups (N+1 fix)
            existing_invoices_by_number = {
                inv.invoice_number: inv for inv in Invoice.query.filter_by(company_id=company_id).all()
            }

            # Process invoices
            for pl_invoice in all_invoices:
                status = pl_invoice.get('status', '')
                is_paid = pl_invoice.get('paid', False)
                invoice_number = pl_invoice.get('invoice_number')

                if not invoice_number:
                    continue

                # Delete paid invoices from local DB (clean slate approach)
                if is_paid or status == 'paid':
                    existing_paid_invoice = existing_invoices_by_number.get(invoice_number)
                    if existing_paid_invoice:
                        db.session.delete(existing_paid_invoice)
                        del existing_invoices_by_number[invoice_number]
                        logger.info(f"Deleted paid invoice {invoice_number} from local database")
                    continue

                # Skip ignored statuses
                if status in self.IGNORED_INVOICE_STATUSES:
                    continue

                # Parse remaining amount
                remaining_str = pl_invoice.get('remaining_amount_with_tax')
                if remaining_str is not None:
                    try:
                        remaining_amount = Decimal(str(remaining_str))
                    except (InvalidOperation, ValueError):
                        remaining_amount = Decimal('0')
                else:
                    remaining_amount = Decimal('0')

                # Skip fully paid (remaining == 0)
                if remaining_amount <= 0:
                    existing_zero = existing_invoices_by_number.get(invoice_number)
                    if existing_zero:
                        db.session.delete(existing_zero)
                        del existing_invoices_by_number[invoice_number]
                        logger.info(f"Deleted zero-balance invoice {invoice_number}")
                    continue

                # Map invoice fields
                invoice_data = self._map_invoice_fields(pl_invoice, company_id, customer_id_to_client)

                if not invoice_data:
                    continue

                existing_invoice = existing_invoices_by_number.get(invoice_data['invoice_number'])

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

            # NOTE: Quota increment is done once in the route handler (pennylane_sync),
            # not here, to avoid triple-counting.

            db.session.commit()

        except Exception as e:
            logger.error(f"Error syncing Pennylane invoices: {e}")
            from app import db
            db.session.rollback()
            raise

        return created_count, updated_count

    # =========================================================================
    # SYNC PAYMENTS
    # =========================================================================

    def sync_payments(self, company_id: int, sync_log_id: Optional[int] = None) -> int:
        """Synchronise les paiements recus depuis Pennylane vers la table ReceivedPayment.

        Strategie: Utiliser le changelog pour detecter les factures modifiees,
        puis recuperer les matched_transactions pour les factures payees.
        Fallback: iterer les factures payees directement.

        Returns:
            int: Nombre d'enregistrements ReceivedPayment crees
        """
        logger.info("=== STARTING PENNYLANE PAYMENT SYNC ===")
        created_count = 0

        try:
            from app import db
            from models import Client, ReceivedPayment

            if not self.connection:
                raise ValueError("No Pennylane connection configured")

            if self.connection.company_id != company_id:
                raise ValueError(
                    f"SECURITE: Cross-company sync attempt. "
                    f"Connection belongs to company {self.connection.company_id} "
                    f"but sync requested for company {company_id}"
                )

            # Build deduplication set from existing payments (projection query for efficiency)
            existing_keys = set(
                f"{row.external_payment_id}|{row.invoice_number}"
                for row in db.session.query(
                    ReceivedPayment.external_payment_id, ReceivedPayment.invoice_number
                ).filter_by(company_id=company_id, source='pennylane').all()
            )

            # Determine start_date for changelog (incremental sync)
            last_payment = db.session.query(
                db.func.max(ReceivedPayment.created_at)
            ).filter_by(
                company_id=company_id,
                source='pennylane'
            ).scalar()

            # Get changed invoice IDs from changelog
            changed_invoice_ids = set()
            start_date = None
            if last_payment:
                start_date = (last_payment - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%SZ')

            try:
                changed_invoice_ids = self._get_changed_invoice_ids(start_date)
                logger.info(f"Changelog returned {len(changed_invoice_ids)} changed invoices")
            except Exception as e:
                logger.warning(f"Changelog unavailable, falling back to direct query: {e}")

            # Fetch paid invoices and their matched transactions
            paid_invoices = self._fetch_paid_invoices(changed_invoice_ids)
            logger.info(f"Found {len(paid_invoices)} paid invoices to process")

            # Build client lookup index
            clients = Client.query.filter_by(company_id=company_id).all()
            client_by_name = {}
            client_by_id = {}
            for c in clients:
                client_by_name[c.name.lower()] = c
                client_by_id[c.id] = c

            # Build pennylane customer_id -> local client index (cached on instance)
            customer_id_to_client = self._get_customer_index(company_id)

            # Process each paid invoice's matched transactions
            for invoice_info in paid_invoices:
                pl_invoice_id = invoice_info['id']
                invoice_number = invoice_info.get('invoice_number', '')
                invoice_date_str = invoice_info.get('date')
                deadline_str = invoice_info.get('deadline')
                total_amount_str = invoice_info.get('amount', '0')

                # Resolve client
                customer_data = invoice_info.get('customer')
                local_client = None
                if customer_data and customer_data.get('id'):
                    local_client_id = customer_id_to_client.get(customer_data['id'])
                    if local_client_id:
                        local_client = client_by_id.get(local_client_id)

                # Get matched transactions for this invoice
                try:
                    mt_response = self.make_api_request(
                        f'customer_invoices/{pl_invoice_id}/matched_transactions'
                    )
                    mt_data = mt_response.json()
                    matched_transactions = mt_data.get('items', [])
                except Exception as e:
                    logger.warning(f"Failed to get matched_transactions for invoice {pl_invoice_id}: {e}")
                    continue

                for mt in matched_transactions:
                    mt_id = mt.get('id')
                    if not mt_id:
                        continue

                    external_payment_id = f"PENNYLANE_MT_{mt_id}"
                    dedup_key = f"{external_payment_id}|{invoice_number}"

                    if dedup_key in existing_keys:
                        continue

                    # Parse payment data
                    payment_date = self._parse_date_str(mt.get('date'))
                    if not payment_date:
                        payment_date = date.today()

                    payment_amount = self._parse_decimal(mt.get('amount', '0'))
                    invoice_date = self._parse_date_str(invoice_date_str)
                    due_date = self._parse_date_str(deadline_str)
                    original_amount = self._parse_decimal(total_amount_str)

                    new_payment = ReceivedPayment(
                        company_id=company_id,
                        client_id=local_client.id if local_client else None,
                        invoice_number=invoice_number,
                        invoice_date=invoice_date,
                        invoice_due_date=due_date,
                        original_invoice_amount=original_amount,
                        payment_date=payment_date,
                        payment_amount=payment_amount,
                        source='pennylane',
                        external_payment_id=external_payment_id,
                        external_invoice_id=str(pl_invoice_id)
                    )
                    db.session.add(new_payment)
                    existing_keys.add(dedup_key)
                    created_count += 1

            db.session.commit()
            logger.info(f"Pennylane payment sync complete: {created_count} payments created")

        except Exception as e:
            logger.error(f"Error syncing Pennylane payments: {e}")
            from app import db
            db.session.rollback()
            raise

        return created_count

    def _get_changed_invoice_ids(self, start_date: Optional[str] = None) -> set:
        """Get IDs of invoices that changed since start_date via changelog

        Args:
            start_date: RFC3339 datetime string, or None for last 4 weeks

        Returns:
            Set of Pennylane invoice IDs that have been inserted or updated
        """
        changed_ids = set()
        cursor = None

        while True:
            params = {'limit': self.CHANGELOG_LIMIT}
            if start_date and not cursor:
                params['start_date'] = start_date
            if cursor:
                params['cursor'] = cursor

            response = self.make_api_request('changelogs/customer_invoices', params=params)
            data = response.json()

            for change in data.get('items', []):
                if change.get('operation') in ('insert', 'update'):
                    changed_ids.add(change['id'])

            if not data.get('has_more', False) or not data.get('next_cursor'):
                break

            cursor = data['next_cursor']

        return changed_ids

    def _fetch_paid_invoices(self, changed_invoice_ids: set) -> List[Dict]:
        """Fetch paid invoices, optionally filtered by changed IDs

        If changed_invoice_ids is provided and non-empty, only fetch those.
        Otherwise, fetch all paid invoices via pagination.
        """
        paid_invoices = []

        if changed_invoice_ids:
            # Fetch specific invoices by ID and check if paid
            for invoice_id in changed_invoice_ids:
                try:
                    response = self.make_api_request(f'customer_invoices/{invoice_id}')
                    invoice = response.json()

                    if invoice.get('paid', False) or invoice.get('status') == 'paid':
                        paid_invoices.append(invoice)
                except Exception as e:
                    logger.warning(f"Failed to fetch invoice {invoice_id}: {e}")
                    continue
        else:
            # Full scan: fetch all paid invoices (exclude drafts)
            cursor = None
            filter_param = json.dumps([{"field": "status", "operator": "not_eq", "value": "draft"}])

            while True:
                params = {
                    'limit': self.PAGE_LIMIT,
                    'filters': filter_param
                }
                if cursor:
                    params['cursor'] = cursor

                response = self.make_api_request('customer_invoices', params=params)
                data = response.json()

                for inv in data.get('items', []):
                    if inv.get('paid', False) or inv.get('status') == 'paid':
                        paid_invoices.append(inv)

                if not data.get('has_more', False) or not data.get('next_cursor'):
                    break

                cursor = data['next_cursor']

        return paid_invoices

    # =========================================================================
    # PDF DOWNLOAD
    # =========================================================================

    def download_invoice_pdf(self, invoice_id_external: str) -> bytes:
        """Download invoice PDF from Pennylane

        Pennylane provides a temporary signed URL (public_file_url) that expires
        after 30 minutes. We fetch the invoice to get the URL, then download the PDF.

        Args:
            invoice_id_external: The Pennylane invoice ID (stored as string)

        Returns:
            bytes: The PDF file content

        Raises:
            ValueError: If connection is not available, token is invalid, or no PDF available
        """
        if not self.connection:
            raise ValueError("Aucune connexion Pennylane configuree")

        # Validate and refresh token if needed
        if not self.connection.is_token_valid():
            logger.info("Pennylane token expired, refreshing before PDF download...")
            if not self.refresh_access_token():
                raise ValueError("Echec du rafraichissement du token Pennylane")

        # Fetch invoice to get the temporary PDF URL
        response = self.make_api_request(f'customer_invoices/{invoice_id_external}')
        invoice_data = response.json()

        public_file_url = invoice_data.get('public_file_url')
        if not public_file_url:
            raise ValueError(
                "Aucun PDF disponible pour cette facture Pennylane. "
                "La facture n'a peut-etre pas encore ete finalisee."
            )

        # SECURITE: Valider le domaine de l'URL pour prevenir les attaques SSRF
        from urllib.parse import urlparse
        parsed_url = urlparse(public_file_url)
        allowed_domains = {
            'app.pennylane.com',
            'storage.pennylane.com',
            'pennylane.com',
        }
        # Accepter aussi les sous-domaines de pennylane.com et les CDN courants
        hostname = parsed_url.hostname or ''
        if not any(hostname == d or hostname.endswith(f'.{d}') for d in allowed_domains):
            # Accepter aussi les URLs S3/CloudFront courantes pour les assets Pennylane
            if not (hostname.endswith('.amazonaws.com') or hostname.endswith('.cloudfront.net')):
                logger.error(f"SECURITE: URL PDF Pennylane non autorisee: {hostname}")
                raise ValueError(f"URL de telechargement non autorisee: {hostname}")

        # Download the PDF from the signed URL (no auth needed)
        session = self._get_session()
        pdf_response = session.get(public_file_url)

        if pdf_response.status_code != 200:
            logger.error(f"Pennylane PDF download failed: HTTP {pdf_response.status_code}")
            pdf_response.raise_for_status()

        content_type = pdf_response.headers.get('Content-Type', '')
        if 'application/pdf' not in content_type and 'application/octet-stream' not in content_type:
            logger.error(f"Expected PDF but got Content-Type: {content_type}")
            raise ValueError("Reponse invalide de Pennylane (attendu: PDF)")

        return pdf_response.content

    # =========================================================================
    # FIELD MAPPING HELPERS
    # =========================================================================

    def _map_customer_fields(self, pl_customer: Dict) -> Dict:
        """Map Pennylane customer fields to local client fields"""
        client_data = {}

        customer_type = pl_customer.get('customer_type', 'company')

        # Name
        if customer_type == 'individual':
            first_name = pl_customer.get('first_name', '')
            last_name = pl_customer.get('last_name', '')
            name = f"{first_name} {last_name}".strip()
            if not name:
                name = 'Client sans nom'
        else:
            name = pl_customer.get('name', 'Client sans nom')

        client_data['name'] = name

        # Code client: priority external_reference > name
        external_ref = pl_customer.get('external_reference')
        if external_ref and external_ref.strip():
            client_data['code_client'] = external_ref.strip()
        else:
            # Generate from name + Pennylane ID for uniqueness
            import re
            clean_name = re.sub(r'[^a-zA-Z0-9]', '', name)[:10].upper()
            pl_id = pl_customer.get('id', 'unknown')
            client_data['code_client'] = f"{clean_name}_{pl_id}"

        # Email (first from array)
        emails = pl_customer.get('emails', [])
        if emails and isinstance(emails, list) and len(emails) > 0:
            client_data['email'] = emails[0]

        # Phone
        phone = pl_customer.get('phone')
        if phone:
            client_data['phone'] = phone

        # Address from billing_address
        billing_address = pl_customer.get('billing_address')
        if billing_address and isinstance(billing_address, dict):
            address_parts = []
            if billing_address.get('address'):
                address_parts.append(billing_address['address'])
            if billing_address.get('postal_code'):
                address_parts.append(billing_address['postal_code'])
            if billing_address.get('city'):
                address_parts.append(billing_address['city'])
            if billing_address.get('country_alpha2'):
                address_parts.append(billing_address['country_alpha2'])
            if address_parts:
                client_data['address'] = ', '.join(address_parts)

        # Payment terms
        payment_conditions = pl_customer.get('payment_conditions')
        if payment_conditions and payment_conditions in self.PAYMENT_CONDITIONS_MAP:
            client_data['payment_terms'] = self.PAYMENT_CONDITIONS_MAP[payment_conditions]

        # Language
        billing_language = pl_customer.get('billing_language', 'fr_FR')
        if billing_language:
            client_data['language'] = billing_language[:2]  # 'fr_FR' -> 'fr'

        return client_data

    def _map_invoice_fields(self, pl_invoice: Dict, company_id: int,
                            customer_id_to_client: Dict) -> Optional[Dict]:
        """Map Pennylane invoice fields to local invoice fields"""
        from models import Client

        # Resolve client from Pennylane customer reference
        customer_data = pl_invoice.get('customer')
        client = None

        if customer_data and customer_data.get('id'):
            pl_customer_id = customer_data['id']
            local_client_id = customer_id_to_client.get(pl_customer_id)
            if local_client_id:
                client = Client.query.get(local_client_id)

        if not client:
            invoice_number = pl_invoice.get('invoice_number', 'unknown')
            logger.warning(f"Client not found for Pennylane invoice {invoice_number}")
            return None

        # Parse amounts (Pennylane returns strings)
        remaining_str = pl_invoice.get('remaining_amount_with_tax', '0')
        amount_due = self._parse_decimal(remaining_str)

        total_str = pl_invoice.get('amount', pl_invoice.get('currency_amount', '0'))
        original_amount = self._parse_decimal(total_str)

        # Parse dates
        invoice_date = self._parse_date_str(pl_invoice.get('date'))
        due_date = self._parse_date_str(pl_invoice.get('deadline'))

        # Fallback for missing dates
        if not invoice_date:
            invoice_date = date.today()
        if not due_date:
            due_date = invoice_date + timedelta(days=30)

        invoice_data = {
            'client_id': client.id,
            'company_id': company_id,
            'invoice_number': pl_invoice.get('invoice_number'),
            'amount': amount_due,
            'original_amount': original_amount,
            'invoice_date': invoice_date,
            'due_date': due_date,
            'invoice_id_external': str(pl_invoice.get('id')),  # Store Pennylane ID for PDF download
        }

        return invoice_data

    def _get_customer_index(self, company_id: int) -> Dict[int, int]:
        """Get cached customer index, building it on first call"""
        if self._customer_index is None:
            self._customer_index = self._build_customer_index(company_id)
        return self._customer_index

    def _build_customer_index(self, company_id: int) -> Dict[int, int]:
        """Build mapping of Pennylane customer_id -> local client_id

        Uses code_client to cross-reference: code_client contains either
        the external_reference or the generated '{NAME}_{pennylane_id}' pattern.
        """
        from models import Client

        index = {}
        clients = Client.query.filter_by(company_id=company_id).all()

        for client in clients:
            code = client.code_client
            if not code:
                continue

            # Try to extract Pennylane ID from code_client pattern: 'NAME_123'
            if '_' in code:
                suffix = code.rsplit('_', 1)[-1]
                try:
                    pl_id = int(suffix)
                    index[pl_id] = client.id
                except (ValueError, TypeError):
                    pass

        # Also try to resolve via a fresh API call if index is empty
        # This handles the case where external_reference was used as code_client
        if not index:
            try:
                all_customers = self._paginate_cursor('customers')
                for pl_customer in all_customers:
                    pl_id = pl_customer.get('id')
                    ext_ref = pl_customer.get('external_reference', '')
                    pl_name = pl_customer.get('name', '')

                    if ext_ref:
                        local_client = Client.query.filter_by(
                            code_client=ext_ref,
                            company_id=company_id
                        ).first()
                        if local_client:
                            index[pl_id] = local_client.id
                            continue

                    # Fallback: match by name
                    local_client = Client.query.filter_by(
                        name=pl_name,
                        company_id=company_id
                    ).first()
                    if local_client:
                        index[pl_id] = local_client.id
            except Exception as e:
                logger.warning(f"Failed to build customer index via API: {e}")

        return index

    def _parse_date_str(self, date_str: Optional[str]) -> Optional[date]:
        """Parse Pennylane date string (ISO 8601) to date object"""
        if not date_str:
            return None

        try:
            # Pennylane uses ISO 8601 format: YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS
            if 'T' in date_str:
                return datetime.strptime(date_str[:10], '%Y-%m-%d').date()
            return datetime.strptime(date_str, '%Y-%m-%d').date()
        except (ValueError, AttributeError) as e:
            logger.warning(f"Failed to parse Pennylane date '{date_str}': {e}")
            return None

    def _parse_decimal(self, value) -> Decimal:
        """Parse a Pennylane amount (string or number) to Decimal"""
        if value is None:
            return Decimal('0')
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return Decimal('0')


def get_pennylane_connector(company_id: int) -> Optional[PennylaneConnector]:
    """Factory function to get a PennylaneConnector for a company

    Args:
        company_id: ID of the company

    Returns:
        PennylaneConnector instance or None if no active connection
    """
    from models import AccountingConnection

    connection = AccountingConnection.query.filter_by(
        company_id=company_id,
        system_type='pennylane',
        is_active=True
    ).first()

    if not connection:
        return None

    return PennylaneConnector(connection_id=connection.id, company_id=company_id)
