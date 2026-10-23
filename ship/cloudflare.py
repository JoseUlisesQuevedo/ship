"""
A small client for the parts of the Cloudflare REST API that ship uses.

Every Cloudflare API response has the same envelope:

    {"success": true, "errors": [], "messages": [], "result": ...}

so `_request` checks `success`, raises CloudflareError with the error messages
if it's false, and otherwise returns just the `result` part.

API reference: https://developers.cloudflare.com/api/
"""

import json

import httpx

API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareError(Exception):
    """Raised when the Cloudflare API says a request failed."""


class Cloudflare:
    def __init__(self, token: str, account_id: str | None = None):
        # account_id can be filled in later: `ship setup` only learns it after
        # asking the API which accounts the token can see.
        self.account_id = account_id
        self.http = httpx.Client(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )

    # ------------------------------------------------------------------ basics

    def _call(self, method: str, path: str, **kwargs) -> dict:
        """Send a request and return the whole response envelope."""
        response = self.http.request(method, path, **kwargs)
        try:
            body = response.json()
        except ValueError:
            raise CloudflareError(f"{method} {path}: HTTP {response.status_code}, not JSON")

        if not body.get("success"):
            messages = "; ".join(e.get("message", "?") for e in body.get("errors", []))
            error = f"{method} {path}: {messages or f'HTTP {response.status_code}'}"
            # 401/403 almost always means the token expired, was revoked, or
            # lacks a permission. Say how to fix it.
            if response.status_code in (401, 403):
                error += "\nThe token may have expired. Run `ship setup` and paste a new one."
            raise CloudflareError(error)
        return body

    def _request(self, method: str, path: str, **kwargs):
        """Send a request and return only its `result`."""
        return self._call(method, path, **kwargs)["result"]

    def _paginate(self, path: str, params: dict | None = None) -> list:
        """Collect every page of a list endpoint (e.g. zones come 50 at a time)."""
        items, page = [], 1
        while True:
            body = self._call("GET", path, params={**(params or {}), "page": page, "per_page": 50})
            items.extend(body["result"])
            if page >= body.get("result_info", {}).get("total_pages", 1):
                return items
            page += 1

    @property
    def _account(self) -> str:
        """URL prefix for account-level endpoints."""
        return f"/accounts/{self.account_id}"

    # --------------------------------------------------------- token & account

    def list_accounts(self) -> list[dict]:
        return self._paginate("/accounts")

    def verify_token(self) -> dict:
        """
        Facts about the token we're authenticating with, e.g.
        {"id": "89aa...", "status": "active", "expires_on": "2027-04-20T23:59:59Z"}.

        The ID, together with the token itself, gives us R2 credentials (see
        storage.py). "expires_on" is missing if the token never expires.

        User-owned and account-owned tokens are verified at different URLs,
        so try one and fall back to the other.
        """
        try:
            return self._request("GET", "/user/tokens/verify")
        except CloudflareError:
            return self._request("GET", f"{self._account}/tokens/verify")

    # ----------------------------------------------------------- zones & DNS

    def list_zones(self) -> list[dict]:
        """Every domain ("zone") in the account."""
        return self._paginate("/zones", {"account.id": self.account_id})

    def find_zone(self, hostname: str) -> dict | None:
        """
        Which of your zones does this hostname belong to?

        "a.b.example.com" belongs to "example.com". If you also had a zone for
        "b.example.com", the longer (more specific) match wins.
        """
        matches = [
            zone for zone in self.list_zones()
            if hostname == zone["name"] or hostname.endswith("." + zone["name"])
        ]
        return max(matches, key=lambda zone: len(zone["name"]), default=None)

    def list_dns_records(self, zone_id: str, hostname: str) -> list[dict]:
        return self._request("GET", f"/zones/{zone_id}/dns_records", params={"name": hostname})

    def delete_dns_record(self, zone_id: str, record_id: str) -> None:
        self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")

    # -------------------------------------------------------------------- R2

    def ensure_bucket(self, name: str) -> bool:
        """Create the R2 bucket unless it already exists. Returns True if created."""
        try:
            self._request("GET", f"{self._account}/r2/buckets/{name}")
            return False
        except CloudflareError:
            self._request("POST", f"{self._account}/r2/buckets", json={"name": name})
            return True

    # --------------------------------------------------------------- Workers

    def upload_worker(self, name: str, script: str, bucket: str) -> None:
        """
        Upload (or replace) the Worker script.

        The upload is a multipart form with two parts:
          * "metadata": JSON describing the script, including its *bindings*.
            The R2 binding is what makes `env.SITES` inside worker.js point
            at our bucket.
          * "worker.js": the code itself, as an ES module.
        """
        metadata = {
            "main_module": "worker.js",
            "compatibility_date": "2025-01-01",
            "bindings": [{"type": "r2_bucket", "name": "SITES", "bucket_name": bucket}],
        }
        self._request(
            "PUT",
            f"{self._account}/workers/scripts/{name}",
            files={
                "metadata": (None, json.dumps(metadata), "application/json"),
                "worker.js": ("worker.js", script, "application/javascript+module"),
            },
        )

    # ------------------------------------------------- Workers Custom Domains
    #
    # A Custom Domain sends ALL traffic for a hostname to a Worker. Attaching
    # one also makes Cloudflare create the DNS record and the HTTPS
    # certificate, which is why ship never has to write DNS records itself.

    def list_worker_domains(self, worker: str) -> list[dict]:
        return self._request("GET", f"{self._account}/workers/domains", params={"service": worker})

    def attach_domain(self, hostname: str, worker: str, zone_id: str) -> None:
        self._request(
            "PUT",
            f"{self._account}/workers/domains",
            json={"hostname": hostname, "service": worker, "zone_id": zone_id},
        )

    def detach_domain(self, domain_id: str) -> None:
        """Remove a Custom Domain. Cloudflare deletes its DNS record too."""
        self._request("DELETE", f"{self._account}/workers/domains/{domain_id}")
