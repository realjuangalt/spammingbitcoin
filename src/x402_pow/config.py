from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
MEMES_DIR = ROOT / "apps" / "demo_site" / "static" / "memes"


def normalize_stratum_host(host: str) -> str:
    """Accept stratum+tcp://host or host; return bare hostname."""
    h = (host or "").strip()
    for prefix in ("stratum+tcp://", "stratum+ssl://", "stratum://", "tcp://"):
        if h.lower().startswith(prefix):
            h = h[len(prefix) :]
            break
    return h.split("/")[0].split(":")[0].strip()


def normalize_network(value: str | None) -> str:
    """Return 'mainnet' or 'testnet'."""
    v = (value or "testnet").strip().lower()
    if v in ("main", "mainnet", "btc", "bitcoin"):
        return "mainnet"
    return "testnet"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = f"sqlite:///{DATA_DIR / 'x402_pow.db'}"
    receipt_secret: str = "dev-change-me-spammingbitcoin"
    public_api_base: str = "http://127.0.0.1:8100"
    public_stratum_host: str = "127.0.0.1"
    public_stratum_port: int = 3333
    # Calibrated for in-browser WebCrypto (~40 kH/s): 2^18 ≈ 6s average
    access_zero_bits: int = 18
    access_max_seconds: int = 3
    upstream_mode: str = "local"  # local | reseller
    # Site-wide chain toggle: testnet | mainnet  (env: NETWORK)
    # Flip this one key, restart API. Selects Upstream profile + public labels.
    network: str = "testnet"
    # Legacy alias — used only if NETWORK is unset/empty in older .env files
    upstream_network: str = ""
    # Testnet4 Upstream (e.g. Xaxa)
    upstream_stratum_host: str = "pool.xaxamining.com"
    upstream_stratum_port: int = 3335
    upstream_stratum_user: str = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
    upstream_stratum_pass: str = "x"
    # Mainnet Upstream (e.g. Braiins)
    upstream_stratum_main_host: str = "stratum.braiins.com"
    upstream_stratum_main_port: int = 3333
    upstream_stratum_main_user: str = ""
    upstream_stratum_main_pass: str = "x"
    domain: str = "spammingbitcoin.com"
    # Pool Stratum edge (Agents / Bitaxe connect here)
    stratum_listen_host: str = "0.0.0.0"
    stratum_listen_port: int = 3333
    # Share difficulty advertised to hardware Agents (Bitaxe-friendly)
    stratum_edge_difficulty: float = 1024.0
    # Site credited when worker user is not pub_….name (set STRATUM_DEFAULT_SITE_ID in .env)
    stratum_default_site_id: str = ""
    # Public signup base (magic links). Empty → https://signup.{domain}
    public_signup_base: str = ""
    # Blink custodial Lightning (Pool treasury payouts)
    blink_api_url: str = "https://api.blink.sv/graphql"
    blink_api_key: str = ""
    blink_wallet_id: str = ""
    # Minimum accrued sats before auto/manual payout to Site LN address
    payout_min_sats: int = 1000
    # Pool commission on Site earnings (disclosed at signup; not a normal 1–2% mining pool)
    pool_fee_percent: int = 21
    # Public source repo (nav link on live site)
    public_github_repo: str = "https://github.com/realjuangalt/spammingbitcoin"

    @property
    def signup_base(self) -> str:
        base = (self.public_signup_base or "").strip().rstrip("/")
        if base:
            return base
        return f"https://signup.{self.domain}"

    @property
    def blink_configured(self) -> bool:
        return bool(self.blink_api_key.strip() and self.blink_wallet_id.strip())

    @property
    def active_network(self) -> str:
        """Resolved site network: mainnet | testnet."""
        primary = (self.network or "").strip()
        if primary:
            return normalize_network(primary)
        return normalize_network(self.upstream_network or "testnet")

    @property
    def is_mainnet(self) -> bool:
        return self.active_network == "mainnet"

    @property
    def bitcoin_network_id(self) -> str:
        """x402 / CAIP-2 style network label."""
        return "bitcoin:mainnet" if self.is_mainnet else "bitcoin:testnet4"

    def active_upstream(self) -> dict[str, str | int]:
        """Resolved Upstream Stratum endpoint for the current network."""
        if self.is_mainnet:
            host = normalize_stratum_host(self.upstream_stratum_main_host)
            return {
                "network": "mainnet",
                "host": host,
                "port": int(self.upstream_stratum_main_port),
                "user": (self.upstream_stratum_main_user or "").strip(),
                "password": self.upstream_stratum_main_pass or "x",
            }
        host = normalize_stratum_host(self.upstream_stratum_host)
        return {
            "network": "testnet",
            "host": host,
            "port": int(self.upstream_stratum_port),
            "user": (self.upstream_stratum_user or "").strip(),
            "password": self.upstream_stratum_pass or "x",
        }


def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEMES_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
