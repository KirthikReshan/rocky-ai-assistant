"""Validated AI preferences; credentials live in Windows Credential Manager."""
from dataclasses import asdict, dataclass, fields
import hashlib
from pathlib import Path
import re

DEFAULT_NIM_MODEL = 'nvidia/nemotron-3.5-super-vl-120b-a12b'


def local_tag(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[\w][\w./:-]{0,140}', value, re.ASCII)
            or 'cloud' in value.casefold() or '://' in value):
        raise ValueError('Use an installed local Ollama model tag, such as qwen3.5:4b.')
    return value


@dataclass(frozen=True)
class ModelSettings:
    chat: str = 'qwen3.5:4b'
    code: str = 'qwen3.5:4b'
    documents: str = 'qwen3.5:4b'
    translation: str = 'translategemma:4b'
    embedding: str = 'embeddinggemma:latest'
    fallback: str = 'qwen3:4b'
    use_index: bool = True
    nim_code: bool = False
    nim_documents: bool = False
    nim_model: str = DEFAULT_NIM_MODEL

    def validate(self):
        for name in ('chat', 'code', 'documents', 'translation', 'embedding', 'fallback'):
            local_tag(getattr(self, name))
        if not all(type(getattr(self, name)) is bool for name in ('use_index', 'nim_code', 'nim_documents')):
            raise ValueError('AI switches must be true or false.')
        if not isinstance(self.nim_model, str) or not re.fullmatch(r'[\w.-]+/[\w.-]+', self.nim_model, re.ASCII):
            raise ValueError('Use an NVIDIA catalog model ID, such as nvidia/nemotron-3.5-super-vl-120b-a12b.')
        return self

    @classmethod
    def load(cls, store):
        saved = store.setting('ai_models', {})
        if not isinstance(saved, dict):
            return cls()
        valid = {field.name for field in fields(cls)}
        try:
            return cls(**{key: value for key, value in saved.items() if key in valid}).validate()
        except (ValueError, TypeError):
            # Corrupt configuration always returns to local operation.
            return cls()

    def save(self, store):
        self.validate()
        store.set_setting('ai_models', asdict(self))


class CredentialVault:
    def __init__(self, database):
        identity = str(Path(database).resolve()).casefold().encode('utf-8')
        self.target = 'Rocky/NVIDIA-NIM/' + hashlib.sha256(identity).hexdigest()[:24]

    def read(self):
        import win32cred
        import pywintypes
        try:
            item = win32cred.CredRead(self.target, win32cred.CRED_TYPE_GENERIC)
        except pywintypes.error as exc:
            if exc.winerror == 1168:
                return ''
            raise OSError('Windows Credential Manager could not read the NVIDIA key.') from None
        try:
            value = item['CredentialBlob']
            if not isinstance(value, (bytes, str)):
                raise ValueError('Invalid credential type')
            return value.decode('utf-16-le') if isinstance(value, bytes) else value
        except (UnicodeError, KeyError, TypeError, ValueError):
            raise OSError('The saved NVIDIA key could not be decoded. Save it again in AI models.') from None

    def save(self, value):
        import win32cred
        if (not isinstance(value, str) or not 10 <= len(value) <= 2000
                or any(character.isspace() for character in value) or not value.isascii()):
            raise ValueError('Enter a valid NVIDIA API key without spaces.')
        try:
            win32cred.CredWrite({'Type': win32cred.CRED_TYPE_GENERIC, 'TargetName': self.target,
                                'UserName': 'Rocky', 'CredentialBlob': value.encode('utf-16-le'),
                                'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE}, 0)
        except Exception:
            raise OSError('Windows Credential Manager could not save the NVIDIA key.') from None

    def delete(self):
        import win32cred
        import pywintypes
        try:
            win32cred.CredDelete(self.target, win32cred.CRED_TYPE_GENERIC)
        except pywintypes.error as exc:
            if exc.winerror != 1168:
                raise OSError('Windows Credential Manager could not remove the NVIDIA key.') from None
