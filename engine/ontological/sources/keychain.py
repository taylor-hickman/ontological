"""Source passwords in the OS keychain, named by reference (ARCHITECTURE.md §4.3).

A server source's spec, and whatever the store keeps of it, hold a `KeychainRef`: the account
the password is filed under. The executor reads the password when it starts a child and hands
it over the pipe, and the child gives it to DuckDB as a temporary, in-memory secret. So it is
never written to disk in plain text, and the store never sees it.

The service is not the one `auto_ontology/infra/keychain.py` files connection JSON under by
database name, so a source's account can't be read back as a connection.
"""

from __future__ import annotations

from dataclasses import dataclass

import keyring
import keyring.errors

SERVICE = "Ontological sources"


class CredentialError(Exception):
    """The keychain has no password for a reference, or couldn't be read."""


@dataclass(frozen=True)
class KeychainRef:
    """Where a password is filed: `account` under `SERVICE`."""

    account: str

    def __post_init__(self) -> None:
        if not self.account:
            raise ValueError("a keychain reference needs an account name")


def save_password(account: str, password: str) -> KeychainRef:
    """File `password` under `account`, replacing any already there, and return its
    reference."""
    ref = KeychainRef(account)
    try:
        keyring.set_password(SERVICE, ref.account, password)
    except keyring.errors.KeyringError as e:
        raise CredentialError(f"couldn't save the password for {account!r}: {e}") from e
    return ref


def read_password(ref: KeychainRef) -> str:
    try:
        password = keyring.get_password(SERVICE, ref.account)
    except keyring.errors.KeyringError as e:
        raise CredentialError(f"couldn't read the keychain for {ref.account!r}: {e}") from e
    if password is None:
        raise CredentialError(f"the keychain has no password for {ref.account!r}")
    return password


def forget_password(ref: KeychainRef) -> None:
    """Remove the password, if there is one."""
    try:
        keyring.delete_password(SERVICE, ref.account)
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError as e:
        raise CredentialError(f"couldn't remove the password for {ref.account!r}: {e}") from e
