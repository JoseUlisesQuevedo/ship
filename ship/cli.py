"""
The `ship` command itself.

    ship setup                              one-time: token, bucket, Worker
    ship page.html demo.example.com         publish (or re-publish) a page
    ship page.html example.com/reports/q3   publish under a path
    ship ./site/ demo.example.com           publish a folder with assets
    ship page.html                          random URL on your default domain
    ship ls                                 list everything published
    ship rm demo.example.com                take a site down

How a publish works, end to end:

    1. Work out the target hostname/path (targets.py).
    2. Make sure that hostname is attached to our Worker as a Custom Domain.
       Cloudflare then creates the DNS record and HTTPS certificate itself.
    3. Upload the files to R2 under "<hostname>/<path>/" (storage.py).
    4. Wait until the URL answers, then print it and copy it to the clipboard.
"""

import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from importlib.resources import files as package_files
from pathlib import Path

import httpx
import typer
from botocore.exceptions import BotoCoreError, ClientError
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from .cloudflare import Cloudflare, CloudflareError
from .config import Config, load_config, load_token, save_config, save_token
from .storage import Storage
from .targets import Target, collect_files, content_type, parse_target, random_slug

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Publish HTML files to your Cloudflare domains.",
)
console = Console()


class Fail(Exception):
    """Stop the command and show the user this message (no stack trace)."""


def connect() -> tuple[Config, Cloudflare, Storage]:
    """Load settings and build the two API clients every command needs."""
    config = load_config()
    token = load_token()
    if config is None or token is None:
        raise Fail("ship isn't set up yet. Run: ship setup")

    cloudflare = Cloudflare(token, config.account_id)
    storage = Storage(config.account_id, config.token_id, token, config.bucket)
    check_token_expiry(config, cloudflare)
    return config, cloudflare, storage


def check_token_expiry(config: Config, cloudflare: Cloudflare) -> None:
    """Warn two weeks before the API token expires, and stop once it has."""
    # Configs written before ship tracked expiry don't have the date yet:
    # ask Cloudflare once and remember the answer.
    if config.token_expires is None:
        config.token_expires = cloudflare.verify_token().get("expires_on", "never")
        save_config(config)

    if config.token_expires == "never":
        return

    expires = datetime.fromisoformat(config.token_expires)
    days_left = (expires - datetime.now(timezone.utc)).days
    if days_left < 0:
        raise Fail(f"Your Cloudflare token expired on {expires:%Y-%m-%d}. {RENEW_TOKEN_HINT}")
    if days_left <= 14:
        console.print(f"[yellow]Your Cloudflare token expires in {days_left} day(s).[/] {RENEW_TOKEN_HINT}")


RENEW_TOKEN_HINT = (
    "Create a new one (or Roll the old one) at https://dash.cloudflare.com/profile/api-tokens, "
    "then run `ship setup` and paste it."
)


# =============================================================== ship publish

@app.command()
def publish(
    source: Path = typer.Argument(..., exists=True, help="An .html file, or a folder with an index.html."),
    target: str | None = typer.Argument(
        None,
        help="sub.example.com or example.com/some/path. Leave out for a random URL on your default domain.",
    ),
    force: bool = typer.Option(False, "--force", help="Replace DNS records that already exist for the hostname."),
):
    """Publish a page. Running it again on the same target replaces the page."""
    config, cloudflare, storage = connect()

    # 1. Where is it going?
    if target:
        where = parse_target(target)
    else:
        where = Target(hostname=config.default_domain, path=random_slug(source.stem))

    # 2. What exactly gets uploaded?
    plan = collect_files(source)
    for warning in plan.warnings:
        console.print(f"[yellow]warning:[/] {warning}")

    # 3. Route the hostname to our Worker (a no-op if it already is).
    ensure_hostname(cloudflare, config, where.hostname, force)

    # 4. Upload. Files overwrite previous versions with the same name. Files
    #    that were in an older version but not in this one are left behind;
    #    they're harmless, and `ship rm` cleans them up.
    with console.status(f"Uploading {len(plan.files)} file(s)..."):
        for local_path, site_path in plan.files:
            storage.upload(where.prefix + site_path, local_path, content_type(local_path))

    # 5. Tell the user, once the page actually answers.
    with console.status(f"Waiting for {where.url} to come online..."):
        live = wait_until_live(where.url, where.hostname)

    copy_to_clipboard(where.url)
    if live:
        console.print(f"[green]Live:[/] {where.url}  [dim](copied to clipboard)[/]")
    else:
        console.print(
            f"[yellow]Uploaded, but not answering yet:[/] {where.url}\n"
            "  A new hostname can take a few minutes to get its DNS record and HTTPS certificate.\n"
            f"  To check from Cloudflare's side:  dig +short {where.hostname} @1.1.1.1"
        )


def ensure_hostname(cloudflare: Cloudflare, config: Config, hostname: str, force: bool) -> None:
    """Attach `hostname` to the Worker as a Custom Domain, unless it already is."""
    attached = {domain["hostname"] for domain in cloudflare.list_worker_domains(config.worker)}
    if hostname in attached:
        return

    zone = cloudflare.find_zone(hostname)
    if zone is None:
        raise Fail(f"{hostname} isn't part of any domain in your Cloudflare account.")

    # Safety check: if the hostname already points somewhere (your real
    # website, an email service, ...), taking it over would break that.
    # Only do it when explicitly asked to with --force.
    existing = cloudflare.list_dns_records(zone["id"], hostname)
    if existing:
        listing = "\n".join(f"  {r['type']:6} {r['name']} -> {r['content']}" for r in existing)
        if not force:
            raise Fail(
                f"{hostname} already has DNS records:\n{listing}\n"
                "Publishing would replace them. Re-run with --force if that's what you want."
            )
        console.print(f"[yellow]Removing existing DNS records (--force):[/]\n{listing}")
        for record in existing:
            cloudflare.delete_dns_record(zone["id"], record["id"])

    with console.status(f"Connecting {hostname} to the Worker..."):
        cloudflare.attach_domain(hostname, config.worker, zone["id"])
    console.print(f"Connected [bold]{hostname}[/] (DNS and HTTPS are handled by Cloudflare)")


def wait_until_live(url: str, hostname: str, timeout_seconds: int = 90) -> bool:
    """
    Poll the URL until it returns 200 OK, or give up after the timeout.

    Careful with brand-new hostnames: if your computer (or router) looks one
    up before its DNS record exists, it caches "doesn't exist" for up to 30
    minutes. The page then looks down even though it's live. So first wait
    until Cloudflare's public DNS has the record, and only then make a
    request that goes through your own DNS.
    """
    deadline = time.monotonic() + timeout_seconds
    while not dns_record_exists(hostname):
        if time.monotonic() > deadline:
            return False
        time.sleep(2)

    while time.monotonic() < deadline:
        try:
            if httpx.get(url, follow_redirects=True, timeout=10).status_code == 200:
                return True
        except httpx.HTTPError:
            pass  # DNS not resolving yet, certificate not ready yet, ...
        time.sleep(3)
    return False


def dns_record_exists(hostname: str) -> bool:
    """Ask Cloudflare's public DNS (1.1.1.1), over HTTPS, whether `hostname` resolves yet."""
    try:
        response = httpx.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": hostname, "type": "A"},
            headers={"Accept": "application/dns-json"},
            timeout=10,
        )
        return bool(response.json().get("Answer"))
    except (httpx.HTTPError, ValueError):
        return False


def copy_to_clipboard(text: str) -> None:
    if shutil.which("pbcopy"):  # macOS's clipboard command
        subprocess.run(["pbcopy"], input=text.encode(), check=False)


# ==================================================================== ship ls

@app.command("ls")
def list_sites():
    """List every published site."""
    config, cloudflare, storage = connect()
    files = storage.list_files()

    # A "site" is any folder in the bucket that has an index.html:
    #   "demo.example.com/index.html"         -> site "demo.example.com/"
    #   "example.com/reports/q3/index.html"   -> site "example.com/reports/q3/"
    site_prefixes = {f["key"].removesuffix("index.html") for f in files if f["key"].endswith("/index.html")}

    # Put each file in the most specific site that contains it, then add up stats.
    sites = {prefix: {"files": 0, "size": 0, "modified": None} for prefix in site_prefixes}
    for file in files:
        containing = [prefix for prefix in site_prefixes if file["key"].startswith(prefix)]
        if not containing:
            continue
        site = sites[max(containing, key=len)]
        site["files"] += 1
        site["size"] += file["size"]
        site["modified"] = max(site["modified"] or file["modified"], file["modified"])

    if not sites:
        console.print("Nothing published yet.")
        return

    table = Table("URL", "Files", "Size", "Updated", box=None)
    for prefix, site in sorted(sites.items(), key=lambda item: item[1]["modified"], reverse=True):
        table.add_row(
            f"https://{prefix}",
            str(site["files"]),
            f"{site['size'] / 1024:,.0f} KB",
            site["modified"].astimezone().strftime("%Y-%m-%d %H:%M"),
        )
    console.print(table)

    # Hostnames still attached to the Worker but with nothing in them.
    attached = {domain["hostname"] for domain in cloudflare.list_worker_domains(config.worker)}
    hosts_with_files = {prefix.split("/")[0] for prefix in site_prefixes}
    for hostname in sorted(attached - hosts_with_files):
        console.print(f"[dim]{hostname} is connected but empty (ship rm {hostname} to disconnect)[/]")


# ==================================================================== ship rm

@app.command("rm")
def remove(
    target: str = typer.Argument(..., help="demo.example.com, or example.com/some/path"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."),
):
    """Take a site down: delete its files, and disconnect the hostname if it's a whole host."""
    config, cloudflare, storage = connect()
    where = parse_target(target)

    keys = [file["key"] for file in storage.list_files(where.prefix)]

    # Removing a whole hostname (no path) also disconnects it from the
    # Worker, which makes Cloudflare delete its DNS record.
    domain = None
    if not where.path:
        domain = next(
            (d for d in cloudflare.list_worker_domains(config.worker) if d["hostname"] == where.hostname),
            None,
        )

    if not keys and domain is None:
        raise Fail(f"Nothing is published at {where.url}")

    summary = f"Delete {len(keys)} file(s) under {where.url}"
    if domain:
        summary += f" and disconnect {where.hostname}"
    if not yes and not Confirm.ask(summary + "?"):
        raise typer.Exit()

    storage.delete(keys)
    if domain:
        cloudflare.detach_domain(domain["id"])
    console.print(f"[green]Removed[/] {where.url}")


# ================================================================= ship setup

TOKEN_INSTRUCTIONS = """\
ship needs a Cloudflare API token. Create one at
  https://dash.cloudflare.com/profile/api-tokens  ->  Create Token  ->  Custom token
with these permissions:
  Account | Workers Scripts    | Edit
  Account | Workers R2 Storage | Edit
  Account | Account Settings   | Read
  Zone    | Zone               | Read
  Zone    | DNS                | Edit
  Zone    | Workers Routes     | Edit
and Zone Resources set to "All zones" (or just the domains you'll publish to).
"""


@app.command()
def setup():
    """One-time setup (safe to re-run): save the token, create the bucket, deploy the Worker."""
    previous = load_config()

    # --- The token -----------------------------------------------------------
    token = load_token()
    if token is None or not Confirm.ask("Use the API token already saved in your Keychain?", default=True):
        console.print(TOKEN_INSTRUCTIONS)
        token = Prompt.ask("Paste the token", password=True).strip()

    cloudflare = Cloudflare(token)

    # --- The account ---------------------------------------------------------
    accounts = cloudflare.list_accounts()
    if not accounts:
        raise Fail("That token can't see any Cloudflare account. Check its permissions.")
    if len(accounts) == 1:
        account = accounts[0]
    else:
        for number, candidate in enumerate(accounts, start=1):
            console.print(f"  {number}. {candidate['name']}")
        choice = Prompt.ask("Which account?", choices=[str(n) for n in range(1, len(accounts) + 1)])
        account = accounts[int(choice) - 1]
    cloudflare.account_id = account["id"]
    console.print(f"Account: [bold]{account['name']}[/]")

    # --- The default domain --------------------------------------------------
    zones = [zone["name"] for zone in cloudflare.list_zones()]
    if not zones:
        raise Fail("No domains found in this account (or the token can't read them).")
    console.print("Your domains: " + ", ".join(zones))
    console.print("Pages published without a target go to a random path on a default hostname.")
    default_domain = Prompt.ask(
        "Default hostname",
        default=previous.default_domain if previous else f"p.{zones[0]}",
    ).lower()
    if cloudflare.find_zone(default_domain) is None:
        raise Fail(f"{default_domain} isn't part of any of your domains.")

    token_info = cloudflare.verify_token()
    config = Config(
        account_id=account["id"],
        token_id=token_info["id"],
        token_expires=token_info.get("expires_on", "never"),
        default_domain=default_domain,
    )

    # --- The bucket and the Worker -------------------------------------------
    with console.status("Creating the R2 bucket..."):
        created = cloudflare.ensure_bucket(config.bucket)
    console.print(f"R2 bucket [bold]{config.bucket}[/]: {'created' if created else 'already exists'}")

    with console.status("Deploying the Worker..."):
        script = (package_files("ship") / "worker.js").read_text()
        cloudflare.upload_worker(config.worker, script, config.bucket)
    console.print(f"Worker [bold]{config.worker}[/]: deployed")

    # --- Check that R2 uploads work with the derived credentials -------------
    with console.status("Checking R2 access..."):
        Storage(config.account_id, config.token_id, token, config.bucket).check_access()

    save_token(token)
    save_config(config)
    console.print("[green]All set.[/] Try: ship some-page.html")


# ======================================================================= main

COMMANDS = {"publish", "ls", "rm", "setup", "--help", "-h"}


def main():
    # Allow `ship page.html demo.example.com` as a shortcut for
    # `ship publish page.html demo.example.com`.
    if len(sys.argv) > 1 and sys.argv[1] not in COMMANDS:
        sys.argv.insert(1, "publish")

    # Turn expected failures into a one-line message instead of a stack trace.
    try:
        app()
    except (Fail, ValueError, CloudflareError) as error:
        console.print(f"[red]error:[/] {error}")
        sys.exit(1)
    except (ClientError, BotoCoreError) as error:
        console.print(f"[red]R2 error:[/] {error}")
        if isinstance(error, ClientError) and error.response["Error"]["Code"] in R2_AUTH_ERRORS:
            console.print("The token may have expired. Run `ship setup` and paste a new one.")
        sys.exit(1)


# The error codes R2 returns when the credentials derived from the token are bad.
R2_AUTH_ERRORS = {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "Unauthorized"}
