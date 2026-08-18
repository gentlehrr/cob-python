"""
cob - the official Python SDK for the COB API (cob.farm)

    import cob
    client = cob.Client("cob_sk_live_...")
    silo = client.create_silo("deal-room")
    receipt = silo.upload(["contract.pdf"], wait=True)
    answer = silo.ask("What's the termination notice period?")
    print(answer.text)
"""
from __future__ import annotations

import os
import sys
import time
from typing import List, Optional

import requests

__version__ = "0.1.1"

_BASE = os.environ.get("COB_API_URL", "https://api.cob.farm")


# ── errors ────────────────────────────────────────────────────────────

class Error(Exception):
    """Base class for all COB errors."""

    def __init__(self, message, status=None, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class AuthError(Error):
    """The API key is missing, malformed, or revoked."""


class NotFound(Error):
    """The silo does not exist or does not belong to this key."""


class SiloBusy(Error):
    """The silo is still ingesting; try again when status is 'complete'."""


class Timeout(Error):
    """wait() exceeded its timeout."""


class InsufficientCredits(Error):
    """The quote exceeded the account balance. Carries the money math."""

    def __init__(self, message, quoted_cents=0, balance_cents=0,
                 shortfall_cents=0, files=None, **kw):
        super().__init__(message, **kw)
        self.quoted_cents = quoted_cents
        self.balance_cents = balance_cents
        self.shortfall_cents = shortfall_cents
        self.files = files or []


class APIError(Error):
    """Any other non-success response from the API."""


# ── response objects ──────────────────────────────────────────────────

class Receipt:
    """What an upload cost. Returned by Silo.upload()."""

    def __init__(self, data):
        self.batch_id = data.get("batch_id")
        self.total_cents = data.get("quoted_cents", 0)
        self.files = data.get("files", [])

    @property
    def total(self):
        """Total as dollars, e.g. 1.39"""
        return self.total_cents / 100

    def __repr__(self):
        return f"<cob.Receipt ${self.total:.2f} for {len(self.files)} file(s)>"


class Answer:
    """A cited answer. Returned by Silo.ask()."""

    def __init__(self, data):
        self.text = data.get("answer", "")
        self.cost_cents = data.get("cost_cents", 0)
        self.conversation_id = data.get("conversation_id")

    def __str__(self):
        return self.text

    def __repr__(self):
        return f"<cob.Answer {len(self.text)} chars, {self.cost_cents}c>"


class Pricing:
    """Current rates. Returned by Client.pricing()."""

    def __init__(self, data):
        self.currency = data.get("currency", "USD")
        self.query_cents = data.get("query_cents")
        self.upload = data.get("upload", {})

    def __repr__(self):
        return (f"<cob.Pricing query={self.query_cents}c "
                f"doc={self.upload.get('per_document_cents')}c "
                f"page={self.upload.get('per_page_cents')}c "
                f"image={self.upload.get('per_image_cents')}c "
                f"table={self.upload.get('per_table_cents')}c>")


# ── client ────────────────────────────────────────────────────────────

class Client:
    """Your connection to the COB API.

    Pass a key, or set the COB_API_KEY environment variable.
    """

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or os.environ.get("COB_API_KEY")
        if not self.api_key:
            raise AuthError("no API key: pass cob.Client(\"cob_sk_live_...\") "
                            "or set COB_API_KEY")
        self.base_url = (base_url or _BASE).rstrip("/")
        self._s = requests.Session()
        self._s.headers["Authorization"] = f"Bearer {self.api_key}"

    # -- http core --
    def _req(self, method, path, **kw):
        r = self._s.request(method, self.base_url + path, timeout=kw.pop("timeout", 900), **kw)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code < 300:
            if isinstance(body, dict) and body.get("error"):
                # mid-stream failure delivered inside a 200 body
                raise APIError(body["error"], status=r.status_code,
                               detail=body.get("detail"))
            return body
        msg = body.get("error") or f"request failed ({r.status_code})"
        detail = body.get("detail")
        if r.status_code == 401:
            raise AuthError(msg, status=401, detail=detail)
        if r.status_code == 404:
            raise NotFound(msg, status=404, detail=detail)
        if r.status_code == 409:
            raise SiloBusy(msg, status=409, detail=detail)
        if r.status_code == 402:
            raise InsufficientCredits(
                msg, status=402,
                quoted_cents=body.get("quoted_cents", body.get("price_cents", 0)),
                balance_cents=body.get("balance_cents", 0),
                shortfall_cents=body.get("shortfall_cents", 0),
                files=body.get("files"))
        raise APIError(msg, status=r.status_code, detail=detail)

    # -- surface --
    def pricing(self) -> Pricing:
        return Pricing(self._req("GET", "/v2/pricing"))

    def silos(self) -> List["Silo"]:
        return [Silo(self, d["silo_id"], _data=d) for d in self._req("GET", "/v2/silos")]

    def create_silo(self, name: str) -> "Silo":
        d = self._req("POST", "/v2/silos", json={"name": name})
        return Silo(self, d["silo_id"], _data=d)

    def get_silo(self, silo_id: str) -> "Silo":
        return Silo(self, silo_id)


# ── silo ──────────────────────────────────────────────────────────────

class Silo:
    """A named, isolated corpus of documents. All the action happens here."""

    def __init__(self, client: Client, silo_id: str, _data=None):
        self._client = client
        self.id = silo_id
        self._data = _data or {}

    # -- live state --
    def refresh(self) -> "Silo":
        self._data = self._client._req("GET", f"/v2/silos/{self.id}")
        return self

    @property
    def name(self):
        return self._data.get("name") or self.refresh()._data.get("name")

    @property
    def status(self) -> str:
        """Live status: 'complete' | 'processing' | 'ingesting' | 'error'"""
        self.refresh()
        return self._data.get("live_status") or self._data.get("status")

    @property
    def progress(self) -> float:
        """0.0-1.0 ingestion progress (1.0 when complete)."""
        self.refresh()
        return float(self._data.get("progress", 1.0))

    @property
    def doc_count(self):
        return self._data.get("doc_count", 0)

    # -- upload: stage + push + commit, one call --
    def upload(self, files: List[str], wait: bool = False, quiet: bool = False) -> Receipt:
        """Upload documents. Quotes the batch, charges your credits, and
        admits the files for ingestion. Raises InsufficientCredits (with the
        exact shortfall) if the quote exceeds your balance - nothing is
        charged in that case.
        """
        names = [os.path.basename(p) for p in files]
        stage = self._client._req("POST", "/v2/documents",
                                  json={"silo_id": self.id, "filenames": names})
        for up, path in zip(stage["upload_urls"], files):
            with open(path, "rb") as f:
                r = requests.post(up["url"], data=up["fields"],
                                  files={"file": (up["filename"], f)}, timeout=900)
            if r.status_code >= 300:
                raise APIError(f"upload failed for {up['filename']} "
                               f"({r.status_code})", status=r.status_code)
        receipt = Receipt(self._client._req(
            "POST", "/v2/documents/commit",
            json={"silo_id": self.id, "batch_id": stage["batch_id"]}))
        if wait:
            self.wait(quiet=quiet)
        return receipt

    # -- wait: block until complete, showing live progress --
    def wait(self, poll: float = 5.0, timeout: Optional[float] = None,
             quiet: bool = False) -> "Silo":
        """Block until ingestion completes. Prints live progress (the same
        numbers the cob.farm console shows) so long runs are visibly alive.
        Pass quiet=True to suppress output.
        """
        start = time.time()
        last = None
        while True:
            self.refresh()
            st = self._data.get("live_status") or self._data.get("status")
            pct = int(float(self._data.get("progress", 0)) * 100)
            if st == "complete":
                if not quiet and last is not None:
                    print(f"\rcob: {self.id} complete (100%)      ")
                return self
            if st == "error":
                raise APIError(f"silo {self.id} ingestion failed", detail=st)
            if not quiet and (st, pct) != last:
                sys.stdout.write(f"\rcob: {self.id} {st}... {pct}%   ")
                sys.stdout.flush()
                last = (st, pct)
            if timeout is not None and time.time() - start > timeout:
                raise Timeout(f"silo {self.id} not complete after {timeout}s "
                              f"(currently {st}, {pct}%)")
            time.sleep(poll)

    # -- ask --
    def ask(self, query: str, conversation_id: Optional[str] = None) -> Answer:
        """Ask the corpus a question. Returns a cited Answer."""
        payload = {"silo_id": self.id, "query": query}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        return Answer(self._client._req("POST", "/v2/chat", json=payload))

    def __repr__(self):
        return f"<cob.Silo {self.id}>"


# ── flat tier: no objects, for one-off scripts ────────────────────────

def upload(api_key: str, silo_id: str, files: List[str], wait: bool = False) -> Receipt:
    """One-call upload without constructing a client."""
    return Client(api_key).get_silo(silo_id).upload(files, wait=wait)


def ask(api_key: str, silo_id: str, query: str) -> str:
    """One-call question without constructing a client. Returns the answer text."""
    return Client(api_key).get_silo(silo_id).ask(query).text
