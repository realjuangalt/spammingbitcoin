from __future__ import annotations

"""Short-lived signed URLs for gated meme bytes."""

from itsdangerous import BadSignature, URLSafeTimedSerializer

from x402_pow.config import get_settings

SALT = "x402-pow-media"
DEFAULT_MAX_AGE = 3600  # 1 hour to view after unlock


def _ser() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().receipt_secret, salt=SALT)


def issue_media_token(*, file: str, receipt_id: str) -> str:
    """Bind a meme filename to a settled receipt."""
    # basename only — never allow path traversal
    name = file.rsplit("/", 1)[-1]
    if not name or ".." in name or "/" in name or "\\" in name:
        raise ValueError("invalid media file")
    return _ser().dumps({"f": name, "r": receipt_id})


def load_media_token(token: str, *, max_age: int = DEFAULT_MAX_AGE) -> dict:
    try:
        data = _ser().loads(token, max_age=max_age)
    except BadSignature as e:
        raise ValueError("invalid or expired media token") from e
    name = str(data.get("f", ""))
    if not name or ".." in name or "/" in name or "\\" in name:
        raise ValueError("invalid media file")
    return {"file": name, "receipt_id": str(data.get("r", ""))}
