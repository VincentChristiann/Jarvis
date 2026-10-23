"""Single-use invitations and operator-approved pairing requests."""
import asyncio
import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass, field

INVITE_TTL = 120
MAX_PENDING = 5


def sas_code(token: str, csr_der: bytes) -> str:
    """6-digit code both sides compute independently; the operator compares them."""
    mac = hmac.new(token.encode(), csr_der, hashlib.sha256).digest()
    n = int.from_bytes(mac[:4], "big") % 1_000_000
    return f"{n // 1000:03d}-{n % 1000:03d}"


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Invite:
    role: str
    expires: float


@dataclass
class Pending:
    id: str
    name: str
    platform: str
    device_class: str
    role: str
    sas: str
    ip: str
    csr_pem: bytes
    event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool | None = None


class PairingManager:
    def __init__(self, clock=time.time):
        self.clock = clock
        self._invites: dict[str, Invite] = {}
        self.pending: dict[str, Pending] = {}

    def create_invite(self, role: str, ttl: int = INVITE_TTL) -> str:
        token = secrets.token_urlsafe(32)
        self._invites[_h(token)] = Invite(role, self.clock() + ttl)
        return token

    def consume(self, token) -> Invite | None:
        """Burn the token on first use, valid or not."""
        if not isinstance(token, str) or len(token) > 128:
            return None
        invite = self._invites.pop(_h(token), None)
        if invite is None or invite.expires < self.clock():
            return None
        return invite

    def add_pending(self, **kw) -> Pending | None:
        if len(self.pending) >= MAX_PENDING:
            return None
        p = Pending(id=uuid.uuid4().hex[:8], **kw)
        self.pending[p.id] = p
        return p

    def resolve(self, pending_id: str, approve: bool) -> bool:
        p = self.pending.get(pending_id)
        if p is None or p.approved is not None:
            return False
        p.approved = approve
        p.event.set()
        return True

    def drop(self, pending_id: str):
        self.pending.pop(pending_id, None)
