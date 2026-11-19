# -*- coding: utf-8 -*-
"""Test setup: a deterministic `queries` config, injected before any import
of geo_tracker / report_docx.

queries.py is gitignored local configuration — tests must not depend on the
user's real domains (or even on the file existing). Installing a fake module
into sys.modules makes `from queries import ...` resolve to it.
"""
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

queries = types.ModuleType("queries")
queries.DOMAINS = ["example.com", "test.org"]
queries.NICHES = {
    "n1": {"title": "Niche One", "primary_domain": "example.com",
           "queries": ["q1", "q2"]},
}
queries.BRAND_ALIASES = {"example.com": ["Example Brand"]}
queries.ARTICLES = {}
sys.modules["queries"] = queries

# Tests never read the developer's real .env: it holds live API keys and
# service settings, and both the CLI (cmd_run) and the service (create_app)
# load it on their own. The one test of .env loading runs in a subprocess
# against its own temporary .env.
import geo_tracker  # noqa: E402 — after the fake `queries` is installed

geo_tracker.load_env = lambda: None


@pytest.fixture(autouse=True)
def _clean_report_branding(monkeypatch):
    """White-label settings must not leak between tests (or in from the
    shell): default-branding assertions need the real defaults. Tests that
    exercise REPORT_BRAND / REPORT_ACCENT set them with their own monkeypatch."""
    monkeypatch.delenv("REPORT_BRAND", raising=False)
    monkeypatch.delenv("REPORT_ACCENT", raising=False)
