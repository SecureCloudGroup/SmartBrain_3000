"""Gmail connection state over the encrypted secret store (H6).

One Gmail account per install (single-user). The OAuth client creds and the
refresh token live in the secret store (AES-GCM at rest); the mailbox address is
stored alongside as recall metadata. "Connected" means a refresh token exists.
"""

from __future__ import annotations

from . import gmail

_CLIENT_ID = "email/gmail/client_id"
_CLIENT_SECRET = "email/gmail/client_secret"
_REFRESH = "email/gmail/refresh_token"
_ADDRESS = "email/gmail/address"


def save_client_creds(store, client_id: str, client_secret: str) -> None:
    """Persist the user-supplied OAuth client id + secret."""
    assert client_id and client_secret, "client id + secret required"
    store.put(_CLIENT_ID, client_id)
    store.put(_CLIENT_SECRET, client_secret)


def save_connection(store, refresh_token: str, address: str) -> None:
    """Persist the refresh token + connected address after a successful OAuth."""
    assert refresh_token and address, "refresh token + address required"
    store.put(_REFRESH, refresh_token)
    store.put(_ADDRESS, address)


def disconnect(store) -> None:
    """Remove all Gmail credentials + state (idempotent)."""
    assert store is not None, "secret store required"
    for key in (_CLIENT_ID, _CLIENT_SECRET, _REFRESH, _ADDRESS):  # fixed, bounded
        store.delete(key)


def status(store) -> dict:
    """Report whether Gmail is connected, plus the address + has-creds flags."""
    assert store is not None, "secret store required"
    return {
        "connected": store.get(_REFRESH) is not None,
        "address": store.get(_ADDRESS),
        "has_creds": store.get(_CLIENT_ID) is not None,
    }


def client_creds(store) -> tuple[str | None, str | None]:
    """Return the stored OAuth client (id, secret) — for reconnecting without re-entry."""
    assert store is not None, "secret store required"
    return store.get(_CLIENT_ID), store.get(_CLIENT_SECRET)


def build_client(store) -> gmail.GmailClient | None:
    """Build a GmailClient from stored creds, or None if not fully connected."""
    assert store is not None, "secret store required"
    client_id, client_secret = store.get(_CLIENT_ID), store.get(_CLIENT_SECRET)
    refresh = store.get(_REFRESH)
    if not (client_id and client_secret and refresh):
        return None
    return gmail.GmailClient(client_id, client_secret, refresh)
