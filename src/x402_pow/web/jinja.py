from __future__ import annotations

from fastapi.templating import Jinja2Templates

from x402_pow.config import ROOT, get_settings


def make_templates() -> Jinja2Templates:
    t = Jinja2Templates(directory=str(ROOT / "templates"))
    t.env.globals["site_home"] = _site_home
    t.env.globals["site_domain"] = _site_domain
    return t


def _site_home() -> str:
    d = get_settings().domain.strip().rstrip("/")
    return f"https://{d}/"


def _site_domain() -> str:
    return get_settings().domain.strip()
