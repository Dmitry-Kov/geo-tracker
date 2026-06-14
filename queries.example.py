# -*- coding: utf-8 -*-
"""
Example GEO tracker config. Copy to queries.py and fill in your domains and queries:

    cp queries.example.py queries.py

queries.py is gitignored — it is local configuration.

NICHES structure:
  "niche_key": {
      "title": "human-readable name for reports",
      "primary_domain": "the domain whose visibility this niche measures",
      "queries": ["query1", "query2", ...],
  }

Every engine answer is checked against ALL domains in DOMAINS (not just the
niche's primary one), so cross-citation in other niches is visible too.

The demo niches below are picked so the first run produces hits right away:
Wikipedia and Stack Overflow are frequently cited by AI engines.
Queries can be in any language — the demo mixes English and Russian.
"""

# All tracked domains (no http/www)
DOMAINS = [
    "wikipedia.org",
    "stackoverflow.com",
    "github.com",
]

NICHES = {
    "knowledge": {
        "title": "General knowledge",
        "primary_domain": "wikipedia.org",
        "queries": [
            "кто изобрёл телефон",
            "what is quantum computing",
            "history of the internet",
        ],
    },
    "dev": {
        "title": "Development",
        "primary_domain": "stackoverflow.com",
        "queries": [
            "how to reverse a list in python",
            "git undo last commit",
            "разница между процессом и потоком",
        ],
    },
}
