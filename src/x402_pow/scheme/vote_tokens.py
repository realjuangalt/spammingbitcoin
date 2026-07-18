from __future__ import annotations

"""Single-use, signed right-to-vote issued after a successful meme unlock.

The token binds the meme that was shown, the achieved proof-of-work (best-share
leading zero bits), and the settled receipt id so a vote can only be cast once
per unlock and always carries the work the voter actually produced.
"""

from itsdangerous import BadSignature, URLSafeTimedSerializer

from x402_pow.config import get_settings

SALT = "x402-pow-vote"
DEFAULT_MAX_AGE = 3600  # 1 hour to decide and vote after unlocking


def _ser() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().receipt_secret, salt=SALT)


def issue_vote_token(
    *,
    receipt_id: str,
    meme_id: str,
    achieved_bits: int,
    publisher_id: str | None = None,
    resource: str | None = None,
) -> str:
    return _ser().dumps(
        {
            "r": receipt_id,
            "m": meme_id,
            "b": int(achieved_bits),
            "p": publisher_id,
            "u": resource,
        }
    )


def load_vote_token(token: str, *, max_age: int = DEFAULT_MAX_AGE) -> dict:
    try:
        data = _ser().loads(token, max_age=max_age)
    except BadSignature as e:
        raise ValueError("invalid or expired vote token") from e
    meme_id = str(data.get("m", ""))
    receipt_id = str(data.get("r", ""))
    if not meme_id or not receipt_id:
        raise ValueError("malformed vote token")
    return {
        "receipt_id": receipt_id,
        "meme_id": meme_id,
        "achieved_bits": int(data.get("b", 0)),
        "publisher_id": data.get("p"),
        "resource": data.get("u"),
    }
