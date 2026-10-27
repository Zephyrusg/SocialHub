import json
from cryptography.fernet import Fernet, MultiFernet
from app.core import HubError
from app.database.models import utcnow


class TokenVault:
    """First key encrypts; remaining keys support controlled rotation."""
    def __init__(self, keys):
        self.cipher = MultiFernet([Fernet(k.encode() if isinstance(k, str) else k) for k in keys])

    def encrypt(self, token_metadata):
        return self.cipher.encrypt(json.dumps(token_metadata).encode()).decode()

    def decrypt(self, credential):
        if credential.revoked or (credential.expires_at and credential.expires_at <= utcnow()):
            raise HubError('token_expired', 'Reconnect the platform account')
        return json.loads(self.cipher.decrypt(credential.ciphertext.encode()))
