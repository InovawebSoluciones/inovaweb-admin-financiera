"""Cifrado autenticado de secretos en reposo (AES-256-GCM).

Helper para cifrar/descifrar credenciales de terceros antes de guardarlas en
la BD. Usa AES-256-GCM (AEAD: confidencialidad + integridad en un solo paso)
de la libreria 'cryptography'. NO usar Fernet: el spec del campo AES_KEY exige
GCM y Fernet es AES-128-CBC+HMAC (no cumple).

Formato del token cifrado (texto, apto para columna TEXT):

    "v1." + base64url(nonce[12 bytes] || ciphertext_con_tag)

El prefijo de version "v1." permite rotar el esquema en el futuro sin ambiguar
los tokens existentes; ademas se liga criptograficamente como associated_data
(AAD) del AEAD para impedir confusion/downgrade de version. El ciphertext de
AESGCM ya incluye el tag de 16 bytes al final, por lo que NO se almacena aparte.

La llave se deriva de get_settings().AES_KEY (SecretStr base64). Se valida en
cada uso que decodifique a EXACTAMENTE 32 bytes (AES-256); de lo contrario se
aborta con ValueError (fail-fast, consistente con el resto de config.py).

Invariante de seguridad: el nonce es ALEATORIO de 12 bytes por cada cifrado
(os.urandom). NUNCA se reusa ni se fija un nonce con la misma llave (reusar
nonce en GCM rompe la confidencialidad y la integridad).
"""

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

# Prefijo de version del formato del token. Permite migraciones futuras.
_VERSION_PREFIX = "v1."
# AAD: liga la version al AEAD (no es secreto; evita confusion entre esquemas).
_AAD = b"caf-secret-v1"
# AES-256 requiere una llave de 32 bytes.
_KEY_LEN = 32
# AES-GCM usa nonce de 96 bits (12 bytes) por recomendacion (NIST SP 800-38D).
_NONCE_LEN = 12


def _key() -> bytes:
    """Decodifica y valida la llave AES-256 desde la configuracion.

    Lee get_settings().AES_KEY (SecretStr con la llave en base64), la decodifica
    y exige que sean EXACTAMENTE 32 bytes (AES-256). Aborta con ValueError si el
    secreto no es base64 valido o no mide 32 bytes.
    """
    raw = get_settings().AES_KEY.get_secret_value()
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("AES_KEY no es base64 valido") from exc
    if len(key) != _KEY_LEN:
        raise ValueError(
            f"AES_KEY debe decodificar a {_KEY_LEN} bytes (AES-256); "
            f"se obtuvieron {len(key)}"
        )
    return key


def encrypt_secret(plaintext: str) -> str:
    """Cifra un secreto con AES-256-GCM y devuelve el token versionado.

    Genera un nonce aleatorio de 12 bytes por cada llamada (jamas reusado),
    cifra con AES-256-GCM (tag de autenticacion incluido en el ciphertext, AAD
    = version) y devuelve "v1." + base64url(nonce || ciphertext_con_tag).

    Lanza ValueError si la llave es invalida o si plaintext no es str.
    """
    if not isinstance(plaintext, str):
        raise ValueError("plaintext debe ser str")
    nonce = os.urandom(_NONCE_LEN)
    ciphertext = AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), _AAD)
    payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
    return _VERSION_PREFIX + payload


def decrypt_secret(token: str) -> str:
    """Descifra un token producido por encrypt_secret y devuelve el texto.

    Valida el prefijo de version, decodifica el payload base64url, separa el
    nonce del ciphertext y verifica el tag de autenticacion (con la misma AAD)
    al descifrar.

    Lanza ValueError si: token no es str, no empieza con "v1.", no es base64url
    valido, es demasiado corto para contener nonce + tag, o si la autenticacion
    falla (token manipulado o llave equivocada). Tambien aborta (ValueError) si
    la llave configurada es invalida.
    """
    if not isinstance(token, str):
        raise ValueError("token debe ser str")
    if not token.startswith(_VERSION_PREFIX):
        raise ValueError("token invalido: falta el prefijo de version 'v1.'")

    payload = token[len(_VERSION_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(payload)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("token invalido: payload no es base64url valido") from exc

    # Debe contener al menos nonce (12) + tag GCM (16) bytes.
    if len(raw) < _NONCE_LEN + 16:
        raise ValueError("token invalido: longitud insuficiente")

    nonce, ciphertext = raw[:_NONCE_LEN], raw[_NONCE_LEN:]
    try:
        plaintext = AESGCM(_key()).decrypt(nonce, ciphertext, _AAD)
    except InvalidTag as exc:
        raise ValueError(
            "token invalido: fallo la autenticacion (manipulado o llave incorrecta)"
        ) from exc
    return plaintext.decode("utf-8")
