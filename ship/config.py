"""
Where ship keeps its settings.

There are two kinds of settings, stored in two different places:

  * Plain settings (account ID, default domain, ...) live in a small JSON file
    at ~/.config/ship/config.json. Nothing in it is secret.

  * The Cloudflare API token IS secret, so it lives in the macOS Keychain
    (via the `keyring` library), never in a file. You can also provide it in
    the SHIP_CLOUDFLARE_TOKEN environment variable, which wins if set.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import keyring

CONFIG_PATH = Path.home() / ".config" / "ship" / "config.json"

# How the token is filed in the Keychain: (service, account) is the lookup key.
KEYCHAIN_SERVICE = "ship"
KEYCHAIN_ACCOUNT = "cloudflare-api-token"
TOKEN_ENV_VAR = "SHIP_CLOUDFLARE_TOKEN"


@dataclass
class Config:
    account_id: str        # Your Cloudflare account ID
    token_id: str          # The API token's ID (public; used to derive R2 credentials)
    default_domain: str    # Where `ship page.html` goes when no target is given
    token_expires: str | None = None  # When the token expires (ISO date), "never", or None if not checked yet
    bucket: str = "ship-sites"     # The R2 bucket holding every published file
    worker: str = "ship-router"    # The Worker that serves those files


def load_config() -> Config | None:
    """Read the config file, or return None if `ship setup` hasn't run yet."""
    if not CONFIG_PATH.exists():
        return None
    return Config(**json.loads(CONFIG_PATH.read_text()))


def save_config(config: Config) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(config), indent=2) + "\n")


def load_token() -> str | None:
    """Find the API token: the environment variable first, then the Keychain."""
    return os.environ.get(TOKEN_ENV_VAR) or keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)


def save_token(token: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, token)
