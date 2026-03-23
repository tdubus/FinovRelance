"""
Service de chiffrement centralise pour donnees sensibles
Supporte AES-256-GCM (nouveau, v2:) et Fernet legacy (gAAAAA) en dual-read
Toute nouvelle ecriture se fait en AES-256-GCM
"""

import os
import base64
import json
import time as _time
from functools import lru_cache
from typing import Any, Optional
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import logging
from constants import ENCRYPTION_KEY_LENGTH

logger = logging.getLogger(__name__)


class EncryptionService:
    """Service de chiffrement pour donnees sensibles en base de donnees"""

    VERSION_FERNET = b'v1:'
    VERSION_AES_GCM = b'v2:'

    _instance = None
    _master_key = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """Initialisation du service de chiffrement"""
        encryption_key = os.environ.get('ENCRYPTION_MASTER_KEY')

        # FIX A6-09 : Erreur fatale si cle manquante en production
        if not encryption_key:
            if os.environ.get('DEV_MODE', 'false').lower() == 'true':
                logger.warning("DEV MODE: Generation d'une cle temporaire")
                encryption_key = Fernet.generate_key().decode()
            else:
                raise RuntimeError("ENCRYPTION_MASTER_KEY must be set in production")

        self._master_key = encryption_key.encode() if isinstance(encryption_key, str) else encryption_key

        # Purger le cache PBKDF2 en cas de reinitialisation (rotation de cle)
        _derive_aes256_key_cached.cache_clear()

        # Verifier que la cle est valide pour Fernet (legacy)
        try:
            Fernet(self._master_key)
        except Exception:
            # Si la cle n'est pas valide Fernet, deriver une cle Fernet pour legacy
            self._master_key = self._derive_key(encryption_key.encode() if isinstance(encryption_key, str) else encryption_key)

    def _derive_key(self, password: bytes, salt: bytes = b'finov_security_2024') -> bytes:
        """Derive une cle Fernet a partir d'un mot de passe (legacy, sel statique pour compatibilite)"""
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=ENCRYPTION_KEY_LENGTH,
            salt=salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password))
        return key

    def _derive_aes256_key(self, field_name: str, entity_id) -> bytes:
        """Derive une cle AES-256 avec cache LRU (TTL 5 min) pour eviter 100k iterations PBKDF2 a chaque appel"""
        cache_epoch = int(_time.time() / 300)  # Invalide le cache toutes les 5 min
        return _derive_aes256_key_cached(
            self._master_key, field_name, str(entity_id or 'global'), cache_epoch
        )

    def _get_field_key(self, field_name: str, entity_id: Optional[int] = None) -> bytes:
        """Genere une cle Fernet unique pour chaque champ/entite (legacy)"""
        if not self._master_key:
            raise ValueError("Cle de chiffrement non initialisee")
        unique_string = f"{field_name}_{entity_id or 'global'}_{self._master_key[:16].decode('utf-8', errors='ignore')}"
        return self._derive_key(self._master_key, unique_string.encode())

    def _encrypt_aes_gcm(self, plaintext: str, field_name: str, entity_id) -> str:
        """Chiffre avec AES-256-GCM"""
        key = self._derive_aes256_key(field_name, entity_id)
        nonce = os.urandom(12)  # 96 bits recommended for GCM
        aesgcm = AESGCM(key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)
        # Format: v2: + base64(nonce + ciphertext)
        payload = base64.urlsafe_b64encode(nonce + ciphertext).decode('utf-8')
        return 'v2:' + payload

    def _decrypt_aes_gcm(self, encrypted: str, field_name: str, entity_id) -> str:
        """Dechiffre AES-256-GCM"""
        payload = base64.urlsafe_b64decode(encrypted[3:])  # Skip 'v2:'
        nonce = payload[:12]
        ciphertext = payload[12:]
        key = self._derive_aes256_key(field_name, entity_id)
        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode('utf-8')

    def _encrypt_fernet(self, value_str: str, field_name: str, entity_id: Optional[int] = None) -> str:
        """Chiffre avec Fernet (legacy, garde pour reference)"""
        field_key = self._get_field_key(field_name, entity_id)
        f = Fernet(field_key)
        encrypted = f.encrypt(value_str.encode())
        return encrypted.decode('utf-8')

    def _decrypt_fernet(self, encrypted_value: str, field_name: str, entity_id: Optional[int] = None) -> str:
        """Dechiffre avec Fernet (legacy, pour lire les anciennes donnees)"""
        field_key = self._get_field_key(field_name, entity_id)
        f = Fernet(field_key)
        decrypted = f.decrypt(encrypted_value.encode('utf-8'))
        return decrypted.decode('utf-8')

    def encrypt_field(self, value: Any, field_name: str, entity_id: Optional[int] = None) -> Optional[str]:
        """
        Chiffre une valeur pour un champ specifique avec AES-256-GCM

        Args:
            value: Valeur a chiffrer (string, dict, etc.)
            field_name: Nom du champ (pour derivation de cle)
            entity_id: ID de l'entite (pour isolation)

        Returns:
            Valeur chiffree ou None si vide
        """
        if value is None or value == '':
            return None

        try:
            # Convertir en string si necessaire
            if isinstance(value, dict):
                value_str = json.dumps(value)
            else:
                value_str = str(value)

            return self._encrypt_aes_gcm(value_str, field_name, entity_id)

        except Exception as e:
            logger.error(f"Erreur lors du chiffrement du champ {field_name}: {e}")
            raise RuntimeError(f"Echec du chiffrement du champ {field_name}")

    def decrypt_field(self, encrypted_value: Optional[str], field_name: str, entity_id: Optional[int] = None) -> Optional[str]:
        """
        Dechiffre une valeur pour un champ specifique (dual-read: AES-256-GCM + Fernet legacy)

        Args:
            encrypted_value: Valeur chiffree
            field_name: Nom du champ (pour derivation de cle)
            entity_id: ID de l'entite (pour isolation)

        Returns:
            Valeur dechiffree ou None si vide/echec
        """
        if not encrypted_value:
            return None

        try:
            if encrypted_value.startswith('v2:'):
                # Nouveau format AES-256-GCM
                return self._decrypt_aes_gcm(encrypted_value, field_name, entity_id)
            elif encrypted_value.startswith('gAAAAA'):
                # Legacy Fernet - decrypt with old method
                return self._decrypt_fernet(encrypted_value, field_name, entity_id)
            else:
                # Plaintext (not encrypted) - retourner directement
                logger.debug(f"Valeur du champ {field_name} semble etre en clair, retour direct")
                return encrypted_value
        except Exception as e:
            # FIX A6-03: logger l'erreur sans details sensibles, retourner None
            logger.error(f"Decryption failed for {field_name}: type error")
            return None

    def encrypt_token(self, token: str, token_type: str, owner_id: int) -> Optional[str]:
        """
        Chiffre un token OAuth ou API

        Args:
            token: Token a chiffrer
            token_type: Type de token (oauth_access, oauth_refresh, stripe, etc.)
            owner_id: ID du proprietaire (user_id ou company_id)

        Returns:
            Token chiffre
        """
        return self.encrypt_field(token, f"token_{token_type}", owner_id)

    def decrypt_token(self, encrypted_token: Optional[str], token_type: str, owner_id: int) -> Optional[str]:
        """
        Dechiffre un token OAuth ou API

        Args:
            encrypted_token: Token chiffre
            token_type: Type de token
            owner_id: ID du proprietaire

        Returns:
            Token dechiffre
        """
        return self.decrypt_field(encrypted_token, f"token_{token_type}", owner_id)

    def is_encrypted(self, value: Optional[str]) -> bool:
        """
        Verifie si une valeur semble etre chiffree

        Args:
            value: Valeur a verifier

        Returns:
            True si la valeur semble chiffree
        """
        if not value or not isinstance(value, str):
            return False

        # AES-256-GCM (nouveau format)
        if value.startswith('v2:'):
            return True

        # Fernet commence toujours par gAAAAA
        if value.startswith('gAAAAA'):
            return True

        return False

    def encrypt_json(self, data: dict, field_name: str = "json_data", entity_id: Optional[int] = None) -> str:
        """
        Chiffre un objet JSON

        Args:
            data: Dictionnaire a chiffrer
            field_name: Nom du champ (optionnel)
            entity_id: ID de l'entite (optionnel)

        Returns:
            JSON chiffre en string
        """
        try:
            if not data:
                return ""

            json_str = json.dumps(data)
            encrypted = self.encrypt_field(json_str, field_name, entity_id)
            return encrypted or ""
        except Exception as e:
            logger.error(f"Erreur chiffrement JSON: {e}")
            return ""

    def decrypt_json(self, encrypted_data: str, field_name: str = "json_data", entity_id: Optional[int] = None) -> Optional[dict]:
        """
        Dechiffre un objet JSON

        Args:
            encrypted_data: JSON chiffre
            field_name: Nom du champ (optionnel)
            entity_id: ID de l'entite (optionnel)

        Returns:
            Dictionnaire dechiffre
        """
        try:
            if not encrypted_data:
                return None

            json_str = self.decrypt_field(encrypted_data, field_name, entity_id)
            if not json_str:
                return None

            return json.loads(json_str)
        except Exception as e:
            logger.error(f"Erreur dechiffrement JSON: {e}")
            return None


@lru_cache(maxsize=512)
def _derive_aes256_key_cached(master_key: bytes, field_name: str, entity_id_str: str, cache_epoch: int) -> bytes:
    """Derivation PBKDF2 cachee (100k iterations) - TTL 5 min via cache_epoch"""
    unique_salt = f"{field_name}_{entity_id_str}".encode()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=unique_salt,
        iterations=100000
    )
    return kdf.derive(master_key)


# Instance singleton
encryption_service = EncryptionService()
