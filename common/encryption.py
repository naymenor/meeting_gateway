import json
import os
from cryptography.fernet import Fernet
from django.conf import settings


def encrypt(value):
    version = settings.CREDENTIAL_ENCRYPTION_KEY_VERSION
    return (
        version
        + ":"
        + Fernet(settings.CREDENTIAL_ENCRYPTION_KEY.encode())
        .encrypt(value.encode())
        .decode()
    )


def decrypt(value):
    version, ciphertext = value.split(":", 1)
    keys = json.loads(os.getenv("CREDENTIAL_ENCRYPTION_PREVIOUS_KEYS", "{}"))
    keys[settings.CREDENTIAL_ENCRYPTION_KEY_VERSION] = (
        settings.CREDENTIAL_ENCRYPTION_KEY
    )
    return Fernet(keys[version].encode()).decrypt(ciphertext.encode()).decode()
