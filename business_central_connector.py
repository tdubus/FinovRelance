"""
Business Central Connector
Handles OAuth authentication and data synchronization with Microsoft Business Central via OData V4 API

This connector implements:
- OAuth 2.0 authentication with Microsoft Azure AD
- Dynamic OData V4 table configuration
- Field mapping and OData filters
- Asynchronous data synchronization (Optimized with Prefetch)
- Automatic token refresh

⚠️ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔒 ATTENTION - CODE EN PRODUCTION COMMERCIALE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
VERSION OPTIMISÉE - PERFORMANCE UPDATE
- Bulk Insert implementation (Incremental & Safe)
- Dead code removal
- Memory optimization
- Session persistence fix
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import logging
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from urllib.parse import urlencode
from flask import url_for
import re
import time
from threading import Lock
import os
import threading
from queue import Queue
from constants import DEFAULT_PAGE_SIZE, HTTP_TIMEOUT_DEFAULT

logger = logging.getLogger(__name__)

VERBOSE_MODE = os.environ.get('BC_VERBOSE_MODE', '0') == '1'
if not VERBOSE_MODE:
    logger.setLevel(logging.INFO)


class AuthenticationError(Exception):
    """Raised when Business Central API returns 401 authentication error"""
    pass


from utils.circuit_breaker import CircuitState, CircuitBreaker  # noqa: E402


class ConnectionSnapshot:
    """Immutable snapshot of connection data for thread-safe prefetch"""

    def __init__(self, connection):
        self.connection_id = connection.id
        self.access_token = connection.access_token
        self.refresh_token = connection.refresh_token if hasattr(
            connection, 'refresh_token') else None
        self.token_expires_at = connection.token_expires_at
        self.company_id = connection.company_id
        self.system_type = connection.system_type

    def is_token_expiring_soon(self, buffer_minutes: int = 5) -> bool:
        if not self.token_expires_at:
            return True
        return self.token_expires_at < datetime.utcnow() + timedelta(
            minutes=buffer_minutes)


class TokenCoordinator:
    """Thread-safe coordinator for token refresh between main and prefetch threads"""

    def __init__(self, initial_snapshot: ConnectionSnapshot):
        from queue import Queue
        from threading import Event

        self.current_snapshot = initial_snapshot
        self.snapshot_queue = Queue(maxsize=1)
        self.refresh_request = Event()
        self.shutdown = Event()

    def request_token_refresh(self,
                              timeout: int = 30
                              ) -> Optional[ConnectionSnapshot]:
        logger.info("Prefetch: Requesting token refresh from main thread")
        self.refresh_request.set()
        try:
            new_snapshot = self.snapshot_queue.get(timeout=timeout)
            self.current_snapshot = new_snapshot
            return new_snapshot
        except Exception as e:
            logger.error(f"Prefetch: Token refresh timeout or error: {e}")
            return None

    def provide_refreshed_snapshot(self, snapshot: ConnectionSnapshot):
        self.current_snapshot = snapshot
        self.snapshot_queue.put(snapshot)
        self.refresh_request.clear()

    def check_refresh_requested(self) -> bool:
        return self.refresh_request.is_set()

    def signal_shutdown(self):
        self.shutdown.set()


class BusinessCentralConnector:
    """Microsoft Business Central API connector using OData V4"""

    AUTHORIZATION_URL = 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize'
    TOKEN_URL = 'https://login.microsoftonline.com/common/oauth2/v2.0/token'
    SCOPES = 'https://api.businesscentral.dynamics.com/user_impersonation offline_access'

    # Optimized page size
    DEFAULT_PAGE_SIZE = 1000

    def __init__(self, connection_id: Optional[int] = None):
        import os
        self.connection = None
        if connection_id:
            from models import AccountingConnection
            self.connection = AccountingConnection.query.get(connection_id)

        self.client_id = os.environ.get('BUSINESS_CENTRAL_CLIENT_ID')
        self.client_secret = os.environ.get('BUSINESS_CENTRAL_CLIENT_SECRET')
        self.circuit_breaker = CircuitBreaker(failure_threshold=5,
                                              recovery_timeout=60)
        self.db_lock = Lock()
        self.metadata_cache = {}

    def get_authorization_url(self, state: str) -> str:
        if not self.client_id:
            raise ValueError("Business Central Client ID not configured")

        redirect_uri = url_for('company.business_central_callback',
                               _external=True)
        params = {
            'client_id': self.client_id,
            'response_type': 'code',
            'redirect_uri': redirect_uri,
            'response_mode': 'query',
            'scope': self.SCOPES,
            'state': state,
            'prompt': 'select_account'
        }
        return f"{self.AUTHORIZATION_URL}?{urlencode(params)}"

    def exchange_code_for_tokens(self, authorization_code: str,
                                 state: str) -> Dict:
        if not self.client_id or not self.client_secret:
            raise ValueError("Business Central credentials not configured")

        redirect_uri = url_for('company.business_central_callback',
                               _external=True)
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'authorization_code',
            'code': authorization_code,
            'redirect_uri': redirect_uri,
            'scope': self.SCOPES
        }

        try:
            from utils.http_client import create_secure_session
            session_http = create_secure_session()
            response = session_http.post(self.TOKEN_URL,
                                         headers=headers,
                                         data=data)
            response.raise_for_status()
            token_data = response.json()

            expires_at = datetime.utcnow() + timedelta(
                seconds=token_data.get('expires_in', 3600))
            return {
                'access_token': token_data['access_token'],
                'refresh_token': token_data.get('refresh_token'),
                'expires_at': expires_at,
                'token_type': token_data.get('token_type', 'Bearer')
            }
        except requests.exceptions.RequestException as e:
            logger.error(
                f"Error exchanging Business Central authorization code: {str(e)}"
            )
            raise

    def refresh_access_token(self) -> bool:
        if not self.connection or not self.connection.refresh_token:
            return False
        if not self.client_id or not self.client_secret:
            return False

        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'refresh_token',
            'refresh_token': self.connection.refresh_token,
            'scope': self.SCOPES
        }

        try:
            from utils.http_client import create_secure_session
            from app import db
            session_http = create_secure_session()
            response = session_http.post(self.TOKEN_URL,
                                         headers=headers,
                                         data=data)
            response.raise_for_status()
            token_data = response.json()

            self.connection.access_token = token_data['access_token']
            if 'refresh_token' in token_data:
                self.connection.refresh_token = token_data['refresh_token']
            self.connection.token_expires_at = datetime.utcnow() + timedelta(
                seconds=token_data.get('expires_in', 3600))

            db.session.commit()
            logger.info(
                f"Successfully refreshed BC token for connection {self.connection.id}"
            )
            return True
        except Exception as e:
            logger.error(f"Error refreshing BC token: {str(e)}")
            return False

    def ensure_valid_token(self) -> bool:
        if not self.connection:
            return False

        from app import db
        from sqlalchemy.orm import object_session
        from models import AccountingConnection

        try:
            session = object_session(self.connection)
            if session is None:
                connection_id = self.connection.id
                reloaded = AccountingConnection.query.get(connection_id)
                if not reloaded:
                    return False
                self.connection = reloaded
        except Exception as e:
            logger.error(f"Error checking connection object: {e}")
            return False

        if self.connection.token_expires_at and \
           self.connection.token_expires_at > datetime.utcnow() + timedelta(minutes=5):
            return True

        return self.refresh_access_token()

    def test_connection(self, odata_urls: Dict[str, str]) -> Tuple[bool, str]:
        if not self.ensure_valid_token():
            return False, "Invalid or expired access token"

        try:
            from utils.http_client import create_business_central_session
            headers = {
                'Authorization': f'Bearer {self.connection.access_token}',
                'Accept': 'application/json'
            }
            session_http = create_business_central_session()

            for table_name, url in odata_urls.items():
                test_url = f"{url}?$top=1"
                response = session_http.get(test_url,
                                            headers=headers,
                                            timeout=30)

                if response.status_code == 401:
                    return False, "Authentication failed."
                elif response.status_code == 404:
                    return False, f"Table '{table_name}' not found at URL: {url}"
                elif response.status_code != 200:
                    return False, f"Error accessing '{table_name}': {response.status_code}"

            return True, "Connection successful"
        except Exception as e:
            logger.error(f"Error testing connection: {str(e)}")
            return False, f"Connection error: {str(e)}"

    def get_table_headers(self,
                          odata_url: str,
                          sample_size: int = 20) -> Optional[Dict]:
        if not self.ensure_valid_token():
            return None

        try:
            from utils.http_client import create_business_central_session

            # Step 1: Metadata
            all_fields = self.get_metadata_fields(odata_url)
            if not all_fields:
                all_fields = self.get_available_fields(odata_url,
                                                       sample_size=50)

            # Step 2: Sample data
            headers = {
                'Authorization': f'Bearer {self.connection.access_token}',
                'Accept': 'application/json'
            }
            sample_url = f"{odata_url}?$top={sample_size}"
            session_http = create_business_central_session()
            response = session_http.get(sample_url,
                                        headers=headers,
                                        timeout=30)
            response.raise_for_status()

            data = response.json()
            sample_records = data.get('value', []) if data else []
            all_fields_info = {}

            for field_name in all_fields:
                all_fields_info[field_name] = {
                    'type': 'text',
                    'sample_value': ''
                }

            for record in sample_records:
                for field_name, field_value in record.items():
                    if field_name.startswith('@'): continue

                    if field_name in all_fields_info:
                        if field_value is not None and not all_fields_info[
                                field_name]['sample_value']:
                            # Type detection logic simplified
                            field_type = 'text'
                            if isinstance(field_value, bool):
                                field_type = 'boolean'
                            elif isinstance(field_value, (int, float)):
                                field_type = 'number'
                            elif 'date' in field_name.lower():
                                field_type = 'date'

                            all_fields_info[field_name]['type'] = field_type
                            all_fields_info[field_name]['sample_value'] = str(
                                field_value)[:100]
                    elif field_name not in all_fields_info:
                        # Add fields discovered in sample but not in metadata
                        all_fields_info[field_name] = {
                            'type':
                            'text',
                            'sample_value':
                            str(field_value)[:100] if field_value else ''
                        }

            return {'fields': all_fields_info, 'sample_data': sample_records}
        except Exception as e:
            logger.error(f"Error getting table headers: {str(e)}")
            return None

    def apply_odata_filter(self, url: str, filter_string: str) -> str:
        if not filter_string: return url
        from urllib.parse import quote
        separator = '&' if '?' in url else '?'
        encoded_filter = quote(filter_string, safe="()")
        return f"{url}{separator}$filter={encoded_filter}"

    def apply_odata_select(self, url: str, field_mapping: Dict = None) -> str:
        try:
            available_fields = self.get_available_fields(url, sample_size=10)
            if not available_fields: return url

            fields_to_select = []
            if field_mapping:
                for _, bc_field in field_mapping.items():
                    if bc_field and isinstance(bc_field, str):
                        if ',' in bc_field:
                            for field in bc_field.split(','):
                                if field.strip() in available_fields:
                                    fields_to_select.append(field.strip())
                        elif bc_field in available_fields:
                            fields_to_select.append(bc_field)

            for key_field in ['No', 'Name', 'Code', 'Number', 'Id']:
                if key_field in available_fields and key_field not in fields_to_select:
                    fields_to_select.append(key_field)

            if fields_to_select:
                from urllib.parse import quote_plus
                select_param = ','.join(sorted(set(fields_to_select)))
                separator = '&' if '?' in url else '?'
                return f"{url}{separator}$select={quote_plus(select_param)}"
            return url
        except Exception as e:
            logger.warning(
                f"Field discovery failed, using full retrieval: {e}")
            return url

    def fetch_data_page(
            self,
            url: str,
            page_size: int = None,
            token_snapshot: Optional[ConnectionSnapshot] = None
    ) -> Optional[Dict]:
        if token_snapshot:
            access_token = token_snapshot.access_token
            is_snapshot_mode = True
        else:
            if not self.ensure_valid_token():
                raise RuntimeError("Token invalid")
            access_token = self.connection.access_token
            is_snapshot_mode = False

        def _fetch_with_retry():
            from utils.http_client import create_business_central_session
            session_http = create_business_central_session()
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Accept': 'application/json;odata.metadata=none'
            }

            final_url = url
            if page_size and '$top=' not in final_url:
                headers['Prefer'] = f'odata.maxpagesize={page_size}'

            attempt = 0
            while attempt <= 5:
                try:
                    response = session_http.get(final_url,
                                                headers=headers,
                                                timeout=60)
                    if response.status_code in (429, 500, 502, 503, 504):
                        attempt += 1
                        time.sleep(min(60, 2**attempt))
                        continue

                    if response.status_code == 401:
                        raise AuthenticationError("Token expired")

                    response.raise_for_status()
                    return response.json()
                except requests.exceptions.Timeout:
                    attempt += 1
                    time.sleep(2**attempt)
                except requests.exceptions.HTTPError as e:
                    if e.response and e.response.status_code == 401:
                        raise AuthenticationError(f"Token expired: {str(e)}")
                    raise

        try:
            return self.circuit_breaker.call_with_circuit_breaker(
                _fetch_with_retry)
        except AuthenticationError:
            if not is_snapshot_mode:
                from auth_error_handler import handle_auth_error
                handle_auth_error(self.connection)
            raise

    def iter_odata_pages_with_prefetch(self,
                                       base_url: str,
                                       filter_string: str = None,
                                       page_size: int = 500,
                                       orderby_field: str = None):
        """Optimized iterator with background prefetching"""
        url = self.apply_odata_filter(
            base_url, filter_string) if filter_string else base_url
        if orderby_field and '$orderby=' not in url:
            separator = '&' if '?' in url else '?'
            url = f"{url}{separator}$orderby={orderby_field}"

        snapshot = ConnectionSnapshot(self.connection)
        coordinator = TokenCoordinator(snapshot)
        page_queue = Queue(maxsize=2)

        class SharedState:

            def __init__(self):
                self.url = url
                self.total_items_fetched = 0

        shared = SharedState()

        def fetch_pages():
            try:
                current_snapshot = coordinator.current_snapshot
                while not coordinator.shutdown.is_set():
                    if current_snapshot.is_token_expiring_soon():
                        new_snapshot = coordinator.request_token_refresh(
                            timeout=300)
                        if new_snapshot: current_snapshot = new_snapshot

                    try:
                        data = self.fetch_data_page(
                            shared.url,
                            page_size=page_size,
                            token_snapshot=current_snapshot)
                    except AuthenticationError:
                        new_snapshot = coordinator.request_token_refresh(
                            timeout=30)
                        if new_snapshot:
                            current_snapshot = new_snapshot
                            data = self.fetch_data_page(
                                shared.url,
                                page_size=page_size,
                                token_snapshot=current_snapshot)
                        else:
                            page_queue.put(None)
                            break

                    if not data or 'value' not in data:
                        page_queue.put(None)
                        break

                    page_items = data['value']
                    if not page_items:
                        page_queue.put(None)
                        break

                    page_queue.put(page_items)
                    shared.total_items_fetched += len(page_items)

                    next_link = data.get('@odata.nextLink') or data.get(
                        'odata.nextLink')
                    if next_link:
                        shared.url = next_link
                    else:
                        # Fallback to skip
                        base = self.apply_odata_filter(
                            base_url,
                            filter_string) if filter_string else base_url
                        separator = '&' if '?' in base else '?'

                        if len(page_items) < page_size:
                            page_queue.put(None)  # Done
                            break

                        shared.url = f"{base}{separator}$skip={shared.total_items_fetched}&$top={page_size}"

            except Exception as e:
                logger.error(f"Prefetch thread error: {e}")
                page_queue.put(None)
            finally:
                coordinator.signal_shutdown()

        threading.Thread(target=fetch_pages, daemon=True).start()

        CHUNK_SIZE = 500
        try:
            while True:
                if coordinator.check_refresh_requested():
                    if self.refresh_access_token():
                        coordinator.provide_refreshed_snapshot(
                            ConnectionSnapshot(self.connection))
                    else:
                        coordinator.signal_shutdown()
                        break

                page = page_queue.get(timeout=120)
                if page is None: break

                if len(page) > CHUNK_SIZE:
                    for i in range(0, len(page), CHUNK_SIZE):
                        yield page[i:i + CHUNK_SIZE]
                else:
                    yield page
        finally:
            coordinator.signal_shutdown()

    def get_metadata_fields(self, odata_url: str) -> List[str]:
        cache_key = odata_url.split('?')[0]
        if cache_key in self.metadata_cache:
            return self.metadata_cache[cache_key]

        try:
            from utils.http_client import create_business_central_session
            import defusedxml.ElementTree as ET
            from urllib.parse import unquote

            base_url = cache_key
            parts = base_url.rstrip('/').split('/')

            service_root_index = -1
            for i, part in enumerate(parts):
                if 'ODataV4' in part or 'odata' in part.lower():
                    service_root_index = i + 1
                    break

            service_root = '/'.join(parts[:service_root_index]
                                    ) if service_root_index > 0 else '/'.join(
                                        parts[:-2])
            metadata_url = f"{service_root}/$metadata"

            headers = {
                'Authorization': f'Bearer {self.connection.access_token}',
                'Accept': 'application/xml'
            }

            session_http = create_business_central_session()
            response = session_http.get(metadata_url,
                                        headers=headers,
                                        timeout=30)

            if response.status_code != 200: return []

            root = ET.fromstring(response.text)
            entity_name = unquote(parts[-1])
            ns = {
                'edmx': 'http://docs.oasis-open.org/odata/ns/edmx',
                'edm': 'http://docs.oasis-open.org/odata/ns/edm'
            }

            best_match = None
            best_score = 0

            for entity_type in root.findall('.//edm:EntityType', ns):
                type_name = entity_type.get('Name', '')
                score = 0
                if type_name == entity_name: score = 1000
                elif entity_name in type_name: score = 800

                if score > best_score:
                    best_score = score
                    best_match = entity_type

            if best_match:
                fields = set()
                for prop in best_match.findall('edm:Property', ns):
                    fields.add(prop.get('Name'))
                for nav in best_match.findall('edm:NavigationProperty', ns):
                    fields.add(nav.get('Name'))
                result = sorted(list(fields))
                self.metadata_cache[cache_key] = result
                return result

            return []
        except Exception:
            return []

    def get_available_fields(self,
                             odata_url: str,
                             sample_size: int = DEFAULT_PAGE_SIZE) -> List[str]:
        metadata = self.get_metadata_fields(odata_url)
        if metadata: return metadata

        # Fallback to sample
        try:
            if not self.ensure_valid_token(): return []
            from utils.http_client import create_business_central_session

            headers = {
                'Authorization': f'Bearer {self.connection.access_token}',
                'Accept': 'application/json'
            }
            response = create_business_central_session().get(
                f"{odata_url}?$top={sample_size}", headers=headers, timeout=HTTP_TIMEOUT_DEFAULT)
            response.raise_for_status()

            data = response.json()
            fields = set()
            if 'value' in data:
                for rec in data['value']:
                    for k in rec.keys():
                        if not k.startswith('@'): fields.add(k)
            return sorted(list(fields))
        except Exception:
            return []

    # --- MAPPERS ---
    def map_customer_fields(self,
                            bc_customer: Dict,
                            field_mapping: Dict,
                            clients_dict: Dict = None) -> Dict:
        client_data = {}
        client_data['code_client'] = self._extract_field_value(
            bc_customer, field_mapping.get('client_code', 'No'))
        client_data['name'] = self._extract_field_value(
            bc_customer, field_mapping.get('client_name', 'Name'))
        client_data['email'] = self._extract_field_value(
            bc_customer, field_mapping.get('client_email', 'E_Mail'))
        client_data['phone'] = self._extract_field_value(
            bc_customer, field_mapping.get('client_phone', 'Phone_No'))

        address_fields = field_mapping.get(
            'client_address',
            'Address,Address_2,City,Post_Code,County,Country_Region_Code')
        if ',' in address_fields:
            parts = []
            for field in address_fields.split(','):
                val = self._extract_field_value(bc_customer, field.strip())
                if val: parts.append(val)
            client_data['address'] = ', '.join(parts)
        else:
            client_data['address'] = self._extract_field_value(
                bc_customer, address_fields)

        client_data['representative_name'] = self._extract_field_value(
            bc_customer,
            field_mapping.get('client_representative', 'Salesperson_Code'))
        client_data['payment_terms'] = self._extract_field_value(
            bc_customer,
            field_mapping.get('client_payment_terms', 'Payment_Terms_Code'))

        parent_code = self._extract_field_value(
            bc_customer, field_mapping.get('client_parent_code'))
        if parent_code and clients_dict:
            parent = clients_dict.get(parent_code)
            if parent: client_data['parent_client_id'] = parent.id

        lang = self._extract_field_value(
            bc_customer, field_mapping.get('client_language', 'Language_Code'))
        client_data['language'] = lang.lower()[:2] if lang else 'fr'
        return client_data

    def map_invoice_fields(self, bc_invoice: Dict,
                           field_mapping: Dict) -> Dict:
        invoice_data = {}
        invoice_data['customer_code'] = self._extract_field_value(
            bc_invoice,
            field_mapping.get('invoice_customer_code', 'Sell_to_Customer_No'))
        invoice_data['invoice_number'] = self._extract_field_value(
            bc_invoice, field_mapping.get('invoice_number', 'No'))

        invoice_data['issue_date'] = self._parse_date(
            self._extract_field_value(
                bc_invoice, field_mapping.get('invoice_date',
                                              'Document_Date')))
        invoice_data['due_date'] = self._parse_date(
            self._extract_field_value(
                bc_invoice, field_mapping.get('invoice_due_date', 'Due_Date')))

        # CORRECTION: invoice_balance (Solde Restant) -> amount (champ obligatoire en DB)
        # C'est le montant restant à payer qui est stocké dans le champ 'amount' de la facture
        invoice_data['amount'] = self._parse_amount(
            self._extract_field_value(
                bc_invoice,
                field_mapping.get('invoice_balance', 'Remaining_Amount')))

        # CORRECTION: invoice_amount (Montant Total) -> original_amount (champ optionnel en DB)
        # C'est le montant total original de la facture avant paiements
        orig_field = field_mapping.get('invoice_amount')
        if orig_field:
            val = self._extract_field_value(bc_invoice, orig_field)
            if val: invoice_data['original_amount'] = self._parse_amount(val)

        ext_id = self._extract_field_value(
            bc_invoice, field_mapping.get('invoice_id_external'))
        if ext_id: invoice_data['invoice_id_external'] = ext_id

        return invoice_data

    def _extract_field_value(self, record: Dict, field_path: str) -> Any:
        if not field_path: return None
        value = record
        for part in field_path.split('.'):
            if isinstance(value, dict) and part in value: value = value[part]
            else: return None
        return value

    def _parse_date(self, date_value: Any) -> Optional[str]:
        if not date_value: return None
        if isinstance(date_value, str):
            if 'T' in date_value: date_value = date_value.split('T')[0]
            elif ' ' in date_value: date_value = date_value.split(' ')[0]
            try:
                datetime.strptime(date_value, '%Y-%m-%d')
                return date_value
            except ValueError:
                return None
        return None

    def _parse_amount(self, amount_value: Any) -> Optional[float]:
        if amount_value is None: return None
        try:
            if isinstance(amount_value, str):
                amount_value = re.sub(r'[^\d.-]', '', amount_value)
            return float(amount_value)
        except (ValueError, TypeError):
            return None

    # --- SYNC METHODS ---
    def sync_customers(self,
                       odata_url: str,
                       filter_string: str = None,
                       field_mapping: Dict = None,
                       sync_log_id: int = None,
                       orderby_field: str = None) -> Dict[str, int]:
        if not self.connection: raise ValueError("No active connection")
        from models import Client, Company, SyncLog, CompanySyncUsage
        from app import db

        # FIXED: Use merge instead of remove to keep connection object active
        self.connection = db.session.merge(self.connection)

        stats = {'created': 0, 'updated': 0, 'errors': 0, 'skipped': 0}
        sync_log = SyncLog.query.get(sync_log_id) if sync_log_id else None

        try:
            company = Company.query.get(self.connection.company_id)
            if not CompanySyncUsage.check_company_sync_limit(company.id):
                raise Exception("Daily sync limit reached")

            if not field_mapping:
                field_mapping = self.connection.get_field_mapping()
            odata_url = self.apply_odata_select(odata_url, field_mapping)

            # Delta logic for customers - use separate timestamp if available, fallback to legacy
            delta_enabled = False
            delta_filter = None

            # Priorité: last_customers_sync_at > last_sync_at (fallback pour compatibilité)
            delta_timestamp = None
            if hasattr(self.connection, 'last_customers_sync_at') and self.connection.last_customers_sync_at:
                delta_timestamp = self.connection.last_customers_sync_at
            elif self.connection.last_sync_at:
                delta_timestamp = self.connection.last_sync_at

            if self.connection.delta_enabled and sync_log and delta_timestamp:
                delta_field = field_mapping.get('delta_field') or self.connection.delta_field
                if delta_field:
                    last_sync = delta_timestamp.strftime('%Y-%m-%dT%H:%M:%SZ')
                    delta_filter = f"{delta_field} ge {last_sync}"
                    delta_enabled = True
                    logger.info(f"✅ Delta sync enabled: {delta_filter}")

            combined = f"({filter_string}) and ({delta_filter})" if filter_string and delta_filter else (
                filter_string or delta_filter)

            # Load existing into RAM
            clients_dict = {}
            for client in Client.query.filter_by(
                    company_id=company.id).yield_per(5000):
                clients_dict[client.code_client] = client

            page_iterator = self.iter_odata_pages_with_prefetch(
                odata_url, combined, 500, orderby_field)

            for customers_page in page_iterator:
                # Check for manual stop request
                if sync_log:
                    db.session.refresh(sync_log)
                    if sync_log.is_stop_requested():
                        sync_log.acknowledge_stop()
                        db.session.commit()
                        break

                # OPTIMISATION: Calculer la capacité restante UNE FOIS par page
                # au lieu de faire COUNT(*) pour chaque nouveau client
                # NOTE: Le code original avait un bug - assert_client_capacity(1) faisait un COUNT(*)
                # qui ne voyait pas les clients non-commités, permettant de dépasser la limite.
                # Ce code corrigé suit les créations avec un compteur interne.
                max_clients = company.get_client_limit()
                if max_clients is None or max_clients == 0:
                    # Plan illimité - pas de limite
                    remaining_capacity = float('inf')
                    capacity_already_full = False
                else:
                    current_count = db.session.query(db.func.count(Client.id)).filter_by(company_id=company.id).scalar()
                    remaining_capacity = max(0, max_clients - current_count)
                    capacity_already_full = remaining_capacity == 0
                    if capacity_already_full:
                        logger.warning(f"🚫 LICENCE PLEINE - {company.name}: {current_count}/{max_clients} clients (plan: {company.get_plan_display_name()})")

                # Traitement des clients de la page
                new_clients_created_this_page = 0
                capacity_warning_logged = False
                for bc_customer in customers_page:
                    try:
                        client_data = self.map_customer_fields(
                            bc_customer, field_mapping, clients_dict)
                        code = client_data.get('code_client')
                        if not code:
                            stats['skipped'] += 1
                            continue

                        existing = clients_dict.get(code)
                        if existing:
                            for k, v in client_data.items():
                                if k != 'code_client' and v is not None:
                                    setattr(existing, k, v)
                            stats['updated'] += 1
                        else:
                            # Nouveau client - vérifier si capacité disponible
                            if new_clients_created_this_page >= remaining_capacity:
                                stats['skipped'] += 1
                                # Log une seule fois quand on atteint la limite
                                if not capacity_warning_logged and not capacity_already_full:
                                    logger.warning(f"🚫 LICENCE ATTEINTE - {company.name}: limite de {max_clients} clients atteinte après création de {new_clients_created_this_page} dans cette page")
                                    capacity_warning_logged = True
                            else:
                                new_client = Client(company_id=company.id,
                                                    **client_data)
                                db.session.add(new_client)
                                clients_dict[code] = new_client
                                stats['created'] += 1
                                new_clients_created_this_page += 1
                    except Exception:
                        stats['errors'] += 1

                # Commit unique par page (données + sync_log)
                if sync_log:
                    sync_log.clients_synced = stats['created'] + stats['updated']
                    sync_log.errors_count = stats['errors']
                db.session.commit()

            self.connection.last_sync_at = datetime.utcnow()
            self.connection.last_customers_sync_at = datetime.utcnow()
            db.session.commit()
            CompanySyncUsage.increment_company_sync_count(company.id)

            if sync_log:
                sync_log.status = 'completed'
                sync_log.completed_at = datetime.utcnow()
                if delta_enabled:
                    sync_log.is_delta_sync = True
                    sync_log.delta_filter = delta_filter
                db.session.commit()

        except Exception as e:
            logger.error(f"Customer sync error: {e}")
            if sync_log:
                sync_log.status = 'failed'
                sync_log.error_message = str(e)
                db.session.commit()
            db.session.rollback()
            raise

        return stats

    def sync_invoices(self,
                      odata_url: str,
                      filter_string: str = None,
                      field_mapping: Dict = None,
                      sync_log_id: int = None,
                      orderby_field: str = None) -> Dict[str, int]:
        """Optimized Invoice Sync: Set-Based Approach + Incremental Bulk Insert"""
        if not self.connection: raise ValueError("No active connection")
        from models import Client, Invoice, Company, SyncLog, CompanySyncUsage
        from app import db

        # FIXED: Use merge instead of remove to keep connection object active
        self.connection = db.session.merge(self.connection)

        stats = {'created': 0, 'updated': 0, 'errors': 0, 'skipped': 0}
        sync_log = SyncLog.query.get(sync_log_id) if sync_log_id else None

        try:
            company = Company.query.get(self.connection.company_id)
            if not CompanySyncUsage.check_company_sync_limit(company.id):
                raise Exception("Daily sync limit reached")

            if not field_mapping:
                field_mapping = self.connection.get_field_mapping()

            # 1. MEMORY LOADING (Set-Based Approach)

            # Load clients (code -> id)
            client_map = {
                c.code_client: c.id
                for c in Client.query.filter_by(
                    company_id=company.id).with_entities(
                        Client.code_client, Client.id).all()
            }

            # Load existing invoices (number -> object) to update them in place
            existing_invoices = {
                inv.invoice_number: inv
                for inv in Invoice.query.filter_by(
                    company_id=company.id).all()
            }
            logger.info(
                f"✅ Loaded {len(client_map)} clients and {len(existing_invoices)} invoices."
            )

            # 2. PAGINATION
            page_iterator = self.iter_odata_pages_with_prefetch(
                odata_url, filter_string, 500, orderby_field)
            to_create_dicts = []  # Accumulate new records for bulk insert
            seen_invoice_numbers = set()  # Track invoices from BC (for cleanup)

            for invoices_page in page_iterator:
                # Manual stop check
                if sync_log:
                    db.session.refresh(sync_log)
                    if sync_log.is_stop_requested():
                        sync_log.acknowledge_stop()
                        db.session.commit()
                        break

                for bc_invoice in invoices_page:
                    try:
                        data = self.map_invoice_fields(bc_invoice,
                                                       field_mapping)
                        inv_num = data.get('invoice_number')
                        cust_code = data.get('customer_code')

                        if not inv_num or not cust_code:
                            stats['skipped'] += 1
                            continue

                        # Track this invoice as seen from BC
                        seen_invoice_numbers.add(inv_num)

                        # Resolve Client ID
                        client_id = client_map.get(cust_code)
                        if not client_id:
                            stats['skipped'] += 1
                            continue

                        if inv_num in existing_invoices:
                            # --- UPDATE: amount (solde restant) est le seul champ mutable ---
                            # Les données de facture sont IMMUABLES une fois créées dans BC
                            # Seul le solde restant (amount) change à mesure que les paiements sont appliqués
                            existing = existing_invoices[inv_num]

                            # PERFORMANCE FIX: Ne mettre à jour QUE si la valeur a changé
                            # Évite des milliers d'UPDATE inutiles qui ralentissent la sync
                            new_amount = data.get('amount')
                            if new_amount is not None:
                                # Comparer en float pour éviter les problèmes de précision Decimal vs float
                                current_amount = float(existing.amount) if existing.amount else 0.0
                                if abs(float(new_amount) - current_amount) > 0.001:
                                    existing.amount = new_amount
                                    stats['updated'] += 1
                                else:
                                    stats['skipped'] += 1  # Pas de changement
                            else:
                                stats['skipped'] += 1
                        else:
                            # --- PREPARE INSERT ---
                            # amount = solde restant à payer (obligatoire)
                            # original_amount = montant total de la facture (optionnel)
                            new_inv = {
                                'company_id':
                                company.id,
                                'client_id':
                                client_id,
                                'invoice_number':
                                inv_num,
                                'invoice_date':
                                data.get('issue_date'),
                                'due_date':
                                data.get('due_date'),
                                'amount':
                                data.get('amount', 0.0),
                                'original_amount':
                                data.get('original_amount'),
                                'invoice_id_external':
                                data.get('invoice_id_external'),
                                'is_paid':
                                False,
                                'created_at':
                                datetime.utcnow()
                            }
                            to_create_dicts.append(new_inv)
                            stats['created'] += 1

                    except Exception:
                        stats['errors'] += 1

                # FIXED: Intermediate Bulk Insert for safety
                if len(to_create_dicts) >= 1000:
                    logger.info(
                        f"🚀 Intermediate Bulk Insert of {len(to_create_dicts)} invoices..."
                    )
                    try:
                        db.session.bulk_insert_mappings(Invoice, to_create_dicts)
                        db.session.commit()
                        to_create_dicts = []  # Reset buffer
                    except Exception as e:
                        logger.error(f"Intermediate bulk insert error: {e}")
                        db.session.rollback()
                        raise

                # Commit updates (existing objects) & save log progress
                db.session.commit()
                if sync_log:
                    sync_log.invoices_synced = stats['created'] + stats[
                        'updated']
                    db.session.commit()

                logger.info(
                    f"Processed page. Queue buffer: {len(to_create_dicts)}")

            # 3. FINAL BULK INSERT (Remaining items)
            if to_create_dicts:
                logger.info(
                    f"🚀 Final Bulk inserting {len(to_create_dicts)} new invoices..."
                )
                try:
                    db.session.bulk_insert_mappings(Invoice, to_create_dicts)
                    db.session.commit()
                except Exception as e:
                    logger.error(f"Final bulk insert error: {e}")
                    db.session.rollback()
                    raise

            # 4. CLEANUP: Delete invoices that are no longer in BC (paid invoices)
            # CRITICAL: Only do cleanup if we actually imported invoices from BC
            if seen_invoice_numbers:
                logger.info(f"🧹 Starting cleanup of paid invoices (imported {len(seen_invoice_numbers)} from BC)...")
                try:
                    invoices_to_delete = Invoice.query.filter_by(
                        company_id=company.id
                    ).filter(
                        ~Invoice.invoice_number.in_(seen_invoice_numbers)
                    ).all()

                    if invoices_to_delete:
                        deleted_count = len(invoices_to_delete)
                        for inv in invoices_to_delete:
                            db.session.delete(inv)
                        db.session.commit()
                        stats['deleted'] = deleted_count
                    else:
                        stats['deleted'] = 0
                except Exception as e:
                    logger.error(f"Cleanup error: {e}")
                    db.session.rollback()
                    # Don't raise - cleanup failure shouldn't fail the entire sync
            else:
                logger.warning("⚠️ Skipping cleanup: No invoices were imported from BC (possible sync error or empty dataset)")
                stats['deleted'] = 0

            # Finalize
            self.connection.last_sync_at = datetime.utcnow()
            self.connection.last_invoices_sync_at = datetime.utcnow()
            db.session.commit()
            CompanySyncUsage.increment_company_sync_count(company.id)

            if sync_log:
                sync_log.invoices_synced = stats['created'] + stats['updated']
                sync_log.status = 'completed'
                sync_log.completed_at = datetime.utcnow()
                db.session.commit()

            logger.info(
                f"✅ Invoice sync done. Created: {stats['created']}, Updated: {stats['updated']}, Deleted: {stats.get('deleted', 0)}"
            )

            # Enregistrer snapshot des CAR (non bloquant)
            try:
                from utils.receivables_snapshot import create_receivables_snapshot
                create_receivables_snapshot(company.id, trigger_type='sync')
            except Exception as snapshot_error:
                logger.warning(f"Snapshot CAR non créé: {snapshot_error}")

        except Exception as e:
            logger.error(f"Invoice sync error: {e}")
            if sync_log:
                sync_log.status = 'failed'
                sync_log.error_message = str(e)
                db.session.commit()
            db.session.rollback()
            raise

        return stats

    def _get_rest_api_base(self, bc_config=None):
        from models import BusinessCentralConfig
        from app import db

        if not bc_config:
            bc_config = BusinessCentralConfig.query.filter_by(
                connection_id=self.connection.id
            ).first()
        if not bc_config or not bc_config.invoices_odata_url:
            raise ValueError("Configuration BC incomplète — invoices_odata_url manquante.")

        match = re.search(
            r'v2\.0/([^/]+)/([^/]+)/ODataV4',
            bc_config.invoices_odata_url
        )
        if not match:
            raise ValueError(
                f"Format URL OData BC non reconnu: {bc_config.invoices_odata_url}"
            )
        tenant_id = match.group(1)
        environment = match.group(2)
        base_api_url = (
            f"https://api.businesscentral.dynamics.com"
            f"/v2.0/{tenant_id}/{environment}/api/v2.0"
        )

        company_guid = bc_config.bc_company_guid
        if not company_guid:
            if not self.ensure_valid_token():
                raise ValueError("Token Business Central invalide ou expiré.")

            from utils.http_client import create_business_central_session
            session_http = create_business_central_session()
            headers = {
                'Authorization': f'Bearer {self.connection.access_token}',
                'Accept': 'application/json'
            }

            odata_company_match = re.search(
                r"/Company\('([^']+)'\)/",
                bc_config.invoices_odata_url
            )
            odata_company_name = odata_company_match.group(1) if odata_company_match else None

            companies_response = session_http.get(
                f"{base_api_url}/companies",
                headers=headers,
                timeout=HTTP_TIMEOUT_DEFAULT
            )
            companies_response.raise_for_status()
            all_companies = companies_response.json().get('value', [])
            if not all_companies:
                raise ValueError("Aucune company trouvée dans l'API BC v2.0.")

            logger.info(
                f"BC REST API companies: {[c.get('name') for c in all_companies]}. "
                f"Recherche: '{odata_company_name}'"
            )

            if odata_company_name:
                matched = next(
                    (c for c in all_companies if c.get('name') == odata_company_name),
                    None
                )
                company_guid = matched['id'] if matched else all_companies[0]['id']
            else:
                company_guid = all_companies[0]['id']

            try:
                bc_config.bc_company_guid = company_guid
                db.session.commit()
                logger.info(f"BC company GUID auto-détecté et sauvegardé: {company_guid}")
            except Exception as e:
                logger.warning(f"Impossible de sauvegarder le company GUID: {e}")

        return base_api_url, company_guid

    def sync_payments(self, company_id: int, sync_log_id: int = None) -> int:
        logger.info("=== STARTING BC PAYMENT SYNC (OData CustomerLedgerEntries) ===")
        if not self.connection:
            raise ValueError("No active BC connection")

        from models import Client, ReceivedPayment, BusinessCentralConfig, SyncLog
        from app import db
        from sqlalchemy import text as sa_text
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        self.connection = db.session.merge(self.connection)
        sync_log = SyncLog.query.get(sync_log_id) if sync_log_id else None

        bc_config = BusinessCentralConfig.query.filter_by(
            connection_id=self.connection.id
        ).first()
        if not bc_config or not bc_config.invoices_odata_url:
            raise ValueError("Configuration BC incomplète — invoices_odata_url manquante.")

        # Dériver l'URL CustomerLedgerEntries depuis invoices_odata_url
        # Ex: .../ODataV4/Company('Nom')/Sales_Invoice
        #  →  .../ODataV4/Company('Nom')/CustomerLedgerEntries
        odata_company_match = re.search(
            r'(.+/Company\([^)]+\))/',
            bc_config.invoices_odata_url
        )
        if not odata_company_match:
            raise ValueError(
                f"Format URL OData BC non reconnu: {bc_config.invoices_odata_url}"
            )
        ledger_url = f"{odata_company_match.group(1)}/CustomerLedgerEntries"
        logger.info(f"BC payment sync: OData endpoint → {ledger_url}")

        created_count = 0
        DATE_MIN = datetime(2000, 1, 1).date()
        date_max = (datetime.utcnow() + timedelta(days=30)).date()

        try:
            client_map = {
                c.code_client: c
                for c in Client.query.filter_by(company_id=company_id).all()
                if c.code_client
            }
            logger.info(f"BC payment sync: {len(client_map)} clients loaded")

            # Lire les paramètres de sync une seule fois
            sync_settings = self.connection.get_sync_settings()
            payments_full_done = sync_settings.get('payments_full_sync_done', False)
            payment_cursor_date = sync_settings.get('payment_sync_cursor_date')  # 'YYYY-MM-DD' ou None

            last_payment_date = (
                db.session.query(db.func.max(ReceivedPayment.payment_date))
                .filter_by(company_id=company_id, source='business_central')
                .scalar()
            )

            # Filtre OData — factures entièrement soldées (Open = false)
            # Closed_at_Date = date à laquelle BC a enregistré le règlement → payment_date
            odata_filter = "Document_Type eq 'Invoice' and Open eq false"

            # --- Stratégie de filtrage ---
            # 1. Si la sync complète est déjà faite (flag=True) → incrémental depuis MAX(payment_date)-2j
            # 2. Si un cursor existe (sync interrompue) → reprise depuis le cursor (résumable)
            # 3. Sinon → historique complet depuis le début
            # Dans tous les cas, on trie par Closed_at_Date asc pour traiter du plus vieux au plus récent
            # → permet un cursor date fiable et évite de re-traiter toujours les données récentes.
            cursor_cutoff_str = None
            if payments_full_done:
                if last_payment_date:
                    cutoff = (last_payment_date - timedelta(days=2)).strftime('%Y-%m-%d')
                    odata_filter = f"{odata_filter} and Closed_at_Date ge {cutoff}"
                    logger.info(f"BC payment sync: incrémental depuis {cutoff}")
                else:
                    logger.info("BC payment sync: historique complet (première sync)")
            elif payment_cursor_date:
                # Reprise depuis cursor avec 1 jour de buffer pour éviter les trous
                cursor_dt = datetime.strptime(payment_cursor_date, '%Y-%m-%d')
                resume_from = (cursor_dt - timedelta(days=1)).strftime('%Y-%m-%d')
                odata_filter = f"{odata_filter} and Closed_at_Date ge {resume_from}"
                cursor_cutoff_str = resume_from
                logger.info(
                    f"BC payment sync: REPRISE depuis cursor {resume_from} "
                    f"(dernier cursor sauvegardé: {payment_cursor_date})"
                )
            else:
                logger.info("BC payment sync: historique complet (première sync ou pas de cursor)")

            # Charger les IDs existants pour dedup — scopé au cursor si disponible
            # (évite de charger 70K IDs inutilement en mode reprise)
            if cursor_cutoff_str and not payments_full_done:
                try:
                    cursor_date_obj = datetime.strptime(cursor_cutoff_str, '%Y-%m-%d').date()
                    existing_ids = set(
                        row[0]
                        for row in ReceivedPayment.query.filter(
                            ReceivedPayment.company_id == company_id,
                            ReceivedPayment.source == 'business_central',
                            ReceivedPayment.payment_date >= cursor_date_obj
                        ).with_entities(ReceivedPayment.external_payment_id).all()
                        if row[0]
                    )
                    logger.info(
                        f"BC payment sync: {len(existing_ids)} existing IDs for dedup "
                        f"(scopés depuis {cursor_cutoff_str})"
                    )
                except Exception as dedup_err:
                    logger.warning(f"BC payment: erreur chargement dedup scopé, fallback complet: {dedup_err}")
                    existing_ids = set(
                        row[0]
                        for row in ReceivedPayment.query.filter_by(
                            company_id=company_id, source='business_central'
                        ).with_entities(ReceivedPayment.external_payment_id).all()
                        if row[0]
                    )
                    logger.info(f"BC payment sync: {len(existing_ids)} existing IDs for dedup (complet)")
            else:
                existing_ids = set(
                    row[0]
                    for row in ReceivedPayment.query.filter_by(
                        company_id=company_id, source='business_central'
                    ).with_entities(ReceivedPayment.external_payment_id).all()
                    if row[0]
                )
                logger.info(f"BC payment sync: {len(existing_ids)} existing IDs for dedup")

            logger.info(f"BC payment sync: filtre OData → {odata_filter}")

            # Keepalive DB — évite coupure SSL PostgreSQL sur longue sync historique
            keepalive_stop = threading.Event()

            def _db_keepalive():
                while not keepalive_stop.wait(90):
                    try:
                        db.session.execute(sa_text("SELECT 1"))
                    except Exception as ka_err:
                        logger.debug(f"BC payment keepalive ignoré: {ka_err}")

            keepalive_thread = threading.Thread(target=_db_keepalive, daemon=True)
            keepalive_thread.start()

            total_fetched = 0
            skip_no_entry = 0
            skip_no_client = 0
            skip_invalid_date = 0
            skip_dedup = 0
            skip_no_invoice = 0
            skip_zero_amount = 0
            sample_logged = False
            had_commit_error = False  # si True, on ne posera pas payments_full_sync_done

            try:
                # orderby=Closed_at_Date asc : du plus vieux au plus récent
                # → cursor date progresse naturellement, reprise possible à tout moment
                page_iter = self.iter_odata_pages_with_prefetch(
                    ledger_url, odata_filter, 500,
                    orderby_field='Closed_at_Date asc'
                )

                for page in page_iter:
                    # Vérification force-stop à chaque page (comme la sync clients/factures)
                    if sync_log:
                        db.session.refresh(sync_log)
                        if sync_log.is_stop_requested():
                            logger.info("BC payment sync: arrêt manuel demandé — interruption propre")
                            sync_log.acknowledge_stop()
                            db.session.commit()
                            return created_count

                    if not sample_logged and page:
                        s = page[0]
                        logger.info(
                            f"BC payment sync: sample keys: {list(s.keys())[:30]}"
                        )
                        logger.info(
                            f"BC payment sync: sample values: "
                            f"{ {k: s.get(k) for k in ['Entry_No','Document_No','Document_Type','Posting_Date','Due_Date','Closed_at_Date','Customer_No','Amount','Open'] if k in s} }"
                        )
                        sample_logged = True

                    total_fetched += len(page)

                    for entry in page:
                        try:
                            # Vérification défensive côté Python — BC OData peut ignorer
                            # le filtre Document_Type si le champ n'est pas filterable
                            # dans le web service. On s'assure de ne traiter que les
                            # factures soldées (Invoice, Open=false).
                            doc_type = self._extract_field_value(entry, 'Document_Type') or ''
                            if str(doc_type).strip() != 'Invoice':
                                continue

                            is_open = self._extract_field_value(entry, 'Open')
                            if is_open is True or str(is_open).lower() == 'true':
                                continue

                            entry_no_raw = self._extract_field_value(entry, 'Entry_No')
                            if not entry_no_raw:
                                skip_no_entry += 1
                                continue
                            entry_no = int(entry_no_raw)

                            ext_id = f"BC_CLE_{entry_no}"
                            if ext_id in existing_ids:
                                skip_dedup += 1
                                continue

                            cust_code = self._extract_field_value(entry, 'Customer_No') or ''
                            if not cust_code:
                                skip_no_client += 1
                                continue
                            client = client_map.get(str(cust_code))
                            if not client:
                                skip_no_client += 1
                                continue

                            # payment_date = Closed_at_Date (date de règlement dans BC)
                            closed_date_str = self._parse_date(
                                self._extract_field_value(entry, 'Closed_at_Date')
                            )
                            if not closed_date_str:
                                skip_invalid_date += 1
                                continue
                            try:
                                payment_date = datetime.strptime(
                                    closed_date_str, '%Y-%m-%d'
                                ).date()
                            except (ValueError, TypeError):
                                skip_invalid_date += 1
                                continue

                            if payment_date < DATE_MIN or payment_date > date_max:
                                skip_invalid_date += 1
                                continue

                            # invoice_date = Posting_Date de la facture
                            posting_raw = self._parse_date(
                                self._extract_field_value(entry, 'Posting_Date')
                            )
                            invoice_date = (
                                datetime.strptime(posting_raw, '%Y-%m-%d').date()
                                if posting_raw else None
                            )

                            # invoice_due_date = Due_Date
                            due_raw = self._parse_date(
                                self._extract_field_value(entry, 'Due_Date')
                            )
                            invoice_due_date = (
                                datetime.strptime(due_raw, '%Y-%m-%d').date()
                                if due_raw else None
                            )

                            # Règle de fallback — une vraie facture soldée a toujours
                            # invoice_date ET invoice_due_date. Si l'un des deux manque,
                            # l'écriture n'est pas une facture (Payment, Credit Memo…)
                            # et ne doit pas entrer dans received_payments.
                            if invoice_date is None or invoice_due_date is None:
                                continue

                            amount_raw = self._extract_field_value(entry, 'Amount')
                            amount = self._parse_amount(amount_raw) or 0.0
                            amount = abs(amount)
                            if amount == 0:
                                skip_zero_amount += 1
                                continue

                            invoice_number = str(
                                self._extract_field_value(entry, 'Document_No') or ''
                            ).strip()
                            if not invoice_number:
                                skip_no_invoice += 1
                                continue

                            stmt = (
                                pg_insert(ReceivedPayment)
                                .values(
                                    company_id=company_id,
                                    client_id=client.id,
                                    invoice_number=invoice_number,
                                    invoice_date=invoice_date,
                                    invoice_due_date=invoice_due_date,
                                    original_invoice_amount=amount,
                                    payment_date=payment_date,
                                    payment_amount=amount,
                                    source='business_central',
                                    external_payment_id=ext_id,
                                    external_invoice_id=invoice_number,
                                    created_at=datetime.utcnow()
                                )
                                .on_conflict_do_nothing(
                                    constraint='uq_received_payment_dedup'
                                )
                            )
                            result = db.session.execute(stmt)
                            # rowcount == 1 → inséré ; 0 → doublon ignoré silencieusement
                            if result.rowcount > 0:
                                existing_ids.add(ext_id)
                                created_count += 1

                        except Exception as row_err:
                            logger.warning(f"BC payment row error: {row_err}")
                            try:
                                db.session.rollback()
                            except Exception:
                                pass
                            continue

                    try:
                        db.session.commit()
                    except Exception as commit_err:
                        logger.warning(f"BC payment page commit error: {commit_err}")
                        db.session.rollback()
                        had_commit_error = True

                    # --- Cursor + heartbeat après chaque page ---
                    # Sauvegarde la date max de la page comme cursor de reprise
                    # et met à jour last_activity_at pour éviter que le monitor tue la sync
                    if not had_commit_error:
                        page_dates = [
                            str(e.get('Closed_at_Date', ''))
                            for e in page
                            if e.get('Closed_at_Date') and str(e.get('Closed_at_Date', '')) > '2000-01-01'
                        ]
                        if page_dates:
                            page_max_date = max(page_dates)
                            try:
                                fresh_settings = self.connection.get_sync_settings()
                                fresh_settings['payment_sync_cursor_date'] = page_max_date
                                self.connection.set_sync_settings(fresh_settings)
                                db.session.commit()
                            except Exception as cursor_err:
                                logger.debug(f"BC payment: cursor save error (non-bloquant): {cursor_err}")
                                try:
                                    db.session.rollback()
                                except Exception:
                                    pass

                    # Heartbeat SyncLog — évite que le sync_monitor détecte un blocage
                    if sync_log:
                        try:
                            sync_log.last_activity_at = datetime.utcnow()
                            sync_log.last_processed_skip = total_fetched
                            sync_log.items_processed = created_count
                            db.session.commit()
                        except Exception as hb_err:
                            logger.debug(f"BC payment: heartbeat error (non-bloquant): {hb_err}")
                            try:
                                db.session.rollback()
                            except Exception:
                                pass

            finally:
                keepalive_stop.set()

            logger.info(
                f"=== BC PAYMENT SYNC COMPLETE: {created_count} créés "
                f"(fetched={total_fetched}, skip_dedup={skip_dedup}, "
                f"skip_no_client={skip_no_client}, skip_invalid_date={skip_invalid_date}, "
                f"skip_no_entry={skip_no_entry}, skip_no_invoice={skip_no_invoice}, "
                f"skip_zero_amount={skip_zero_amount}) ==="
            )

            # Marquer la sync historique complète — les prochaines syncs seront
            # incrémentales depuis MAX(payment_date). Le cursor est effacé car inutile.
            # Ce flag reste False (et le cursor est conservé) si :
            #   - sync interrompue (force-stop, exception, redéploiement)
            #   - au moins un commit de page a échoué (données potentiellement manquantes)
            # → Prochain run : reprise automatique depuis le cursor
            if not had_commit_error:
                try:
                    settings = self.connection.get_sync_settings()
                    settings['payments_full_sync_done'] = True
                    settings.pop('payment_sync_cursor_date', None)  # Cursor inutile après sync complète
                    self.connection.set_sync_settings(settings)
                    db.session.commit()
                    logger.info("BC payment sync: payments_full_sync_done=True, cursor effacé")
                except Exception as flag_err:
                    logger.warning(f"BC payment sync: impossible de sauvegarder payments_full_sync_done: {flag_err}")
            else:
                logger.warning(
                    "BC payment sync: payments_full_sync_done NON posé "
                    "(erreur(s) de commit — prochain run reprendra depuis le cursor)"
                )

            return created_count

        except Exception as e:
            logger.error(f"Error during BC payment sync: {e}", exc_info=True)
            try:
                db.session.rollback()
            except Exception:
                pass
            return created_count

    def download_invoice_pdf(self, invoice_number: str) -> bytes:
        """Télécharge le PDF d'une facture de vente reportée ou d'un avoir depuis BC.

        Utilise l'API REST BC v2.0. Recherche par numéro de document (invoice_number)
        via $filter, car invoice_id_external est NULL pour BC (champ non mappé).
        Tente salesInvoices d'abord, puis salesCreditMemos en fallback.

        Args:
            invoice_number: Numéro du document (Document_No) stocké dans invoices.invoice_number.

        Returns:
            Contenu binaire du PDF.

        Raises:
            ValueError: Si le document est introuvable ou si l'API est inaccessible.
        """
        if not self.ensure_valid_token():
            raise ValueError("Token Business Central invalide ou expiré.")

        from models import BusinessCentralConfig
        bc_config = BusinessCentralConfig.query.filter_by(
            connection_id=self.connection.id
        ).first()
        if not bc_config or not bc_config.invoices_odata_url:
            raise ValueError("Configuration BC incomplète — OData URL manquante.")

        match = re.search(
            r'v2\.0/([^/]+)/([^/]+)/ODataV4',
            bc_config.invoices_odata_url
        )
        if not match:
            raise ValueError(
                f"Format URL OData BC non reconnu: {bc_config.invoices_odata_url}"
            )
        tenant_id = match.group(1)
        environment = match.group(2)
        base_api_url = (
            f"https://api.businesscentral.dynamics.com"
            f"/v2.0/{tenant_id}/{environment}/api/v2.0"
        )

        from utils.http_client import create_business_central_session
        session_http = create_business_central_session()
        headers = {
            'Authorization': f'Bearer {self.connection.access_token}',
            'Accept': 'application/json'
        }

        company_id = bc_config.bc_company_guid

        if not company_id:
            # Auto-détection par nom de company extrait de l'URL OData
            odata_company_match = re.search(
                r"/Company\('([^']+)'\)/",
                bc_config.invoices_odata_url
            )
            odata_company_name = odata_company_match.group(1) if odata_company_match else None

            companies_response = session_http.get(
                f"{base_api_url}/companies",
                headers=headers,
                timeout=HTTP_TIMEOUT_DEFAULT
            )
            companies_response.raise_for_status()
            all_companies = companies_response.json().get('value', [])
            if not all_companies:
                raise ValueError("Aucune company trouvée dans l'API BC v2.0.")

            logger.info(
                f"BC companies disponibles: {[c.get('name') for c in all_companies]}. "
                f"Recherche company OData: '{odata_company_name}'"
            )

            if odata_company_name:
                matched = next(
                    (c for c in all_companies if c.get('name') == odata_company_name),
                    None
                )
                company_id = matched['id'] if matched else all_companies[0]['id']
            else:
                company_id = all_companies[0]['id']

            # Sauvegarder le GUID détecté pour affichage dans le formulaire de mapping
            try:
                from app import db
                bc_config.bc_company_guid = company_id
                db.session.commit()
                logger.info(f"BC company GUID auto-détecté et sauvegardé: {company_id}")
            except Exception as e:
                logger.warning(f"Impossible de sauvegarder le company GUID: {e}")

        logger.info(f"BC PDF — company GUID: {company_id}, document: {invoice_number}")

        # Échapper les apostrophes pour OData (standard: ' → '')
        safe_number = invoice_number.replace("'", "''")

        invoice_guid = None
        document_type = None

        for endpoint in ['salesInvoices', 'salesCreditMemos']:
            search_url = (
                f"{base_api_url}/companies({company_id})/{endpoint}"
                f"?$filter=number eq '{safe_number}'"
                f"&$select=id,number"
            )
            logger.info(
                f"BC PDF search — endpoint: {endpoint}, "
                f"company: {company_id}, document: {invoice_number}"
            )
            search_resp = session_http.get(search_url, headers=headers, timeout=HTTP_TIMEOUT_DEFAULT)
            logger.info(f"BC PDF search HTTP {search_resp.status_code} — {endpoint}")
            search_resp.raise_for_status()
            results = search_resp.json().get('value', [])
            logger.info(f"BC PDF search résultats: {len(results)} dans {endpoint}")
            if results:
                invoice_guid = results[0]['id']
                document_type = endpoint
                logger.info(
                    f"Document {invoice_number} trouvé dans {endpoint} "
                    f"(GUID: {invoice_guid})"
                )
                break

        if not invoice_guid:
            raise ValueError(
                f"Document '{invoice_number}' introuvable dans l'API BC "
                f"(salesInvoices et salesCreditMemos consultés). "
                f"Vérifier que le document est reporté dans Business Central."
            )

        # Tentative directe : /pdfDocument/pdfDocumentContent sans appel metadata préalable
        # Économise un round-trip API (~1-2s) dans le cas nominal.
        # Fallback sur l'approche deux étapes (GET /pdfDocument → mediaReadLink) si 404.
        direct_url = (
            f"{base_api_url}/companies({company_id})"
            f"/{document_type}({invoice_guid})"
            f"/pdfDocument/pdfDocumentContent"
        )
        direct_resp = session_http.get(
            direct_url,
            headers={**headers, 'Accept': 'application/pdf'},
            timeout=HTTP_TIMEOUT_DEFAULT
        )
        logger.info(f"BC PDF direct HTTP {direct_resp.status_code}")

        if direct_resp.status_code == 200:
            return direct_resp.content

        if direct_resp.status_code != 404:
            raise ValueError(
                f"Échec du téléchargement PDF BC: HTTP {direct_resp.status_code} "
                f"pour le document '{invoice_number}'."
            )

        # Fallback deux étapes : GET /pdfDocument → extraire mediaReadLink → GET contenu
        logger.info("BC PDF direct 404 — fallback deux étapes")
        pdf_meta_url = (
            f"{base_api_url}/companies({company_id})"
            f"/{document_type}({invoice_guid})/pdfDocument"
        )
        pdf_meta = session_http.get(pdf_meta_url, headers=headers, timeout=HTTP_TIMEOUT_DEFAULT)
        logger.info(f"BC pdfDocument metadata HTTP {pdf_meta.status_code}")
        pdf_meta.raise_for_status()

        pdf_meta_json = pdf_meta.json()

        if 'value' in pdf_meta_json:
            pdf_doc_list = pdf_meta_json['value']
        elif 'id' in pdf_meta_json:
            pdf_doc_list = [pdf_meta_json]
        else:
            pdf_doc_list = []

        if not pdf_doc_list:
            raise ValueError(
                f"Aucun pdfDocument disponible pour '{invoice_number}' dans BC. "
                f"Vérifier que la facture est reportée (statut 'Posted')."
            )

        pdf_doc_entry = pdf_doc_list[0]
        media_link = (
            pdf_doc_entry.get('pdfDocumentContent@odata.mediaReadLink')
            or pdf_doc_entry.get('content@odata.mediaReadLink')
        )

        pdf_content_url = media_link or (
            f"{base_api_url}/companies({company_id})"
            f"/{document_type}({invoice_guid})"
            f"/pdfDocument/pdfDocumentContent"
        )

        fallback_resp = session_http.get(
            pdf_content_url,
            headers={**headers, 'Accept': 'application/pdf'},
            timeout=HTTP_TIMEOUT_DEFAULT
        )
        logger.info(f"BC PDF fallback HTTP {fallback_resp.status_code}")

        if fallback_resp.status_code != 200:
            raise ValueError(
                f"Échec du téléchargement PDF BC: HTTP {fallback_resp.status_code} "
                f"pour le document '{invoice_number}'."
            )

        return fallback_resp.content
