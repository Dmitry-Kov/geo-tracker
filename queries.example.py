# -*- coding: utf-8 -*-
"""
Пример конфига GEO-трекера. Скопируй в queries.py и впиши свои домены и запросы:

    cp queries.example.py queries.py

queries.py в гит не попадает (.gitignore) — это локальная конфигурация.

Структура NICHES:
  "ключ_ниши": {
      "title": "человекочитаемое имя для отчёта",
      "primary_domain": "домен, чью видимость в этой нише меряем",
      "queries": ["запрос1", "запрос2", ...],
  }

В каждом ответе движка проверяются ВСЕ домены из DOMAINS (а не только primary),
поэтому видно и кросс-цитирование доменов в чужих нишах.

Демо-ниши ниже подобраны так, чтобы первый прогон сразу дал попадания:
Wikipedia и Stack Overflow часто цитируются ИИ.
"""

# Все отслеживаемые домены (без http/www)
DOMAINS = [
    "wikipedia.org",
    "stackoverflow.com",
    "github.com",
]

NICHES = {
    "knowledge": {
        "title": "Общие знания",
        "primary_domain": "wikipedia.org",
        "queries": [
            "кто изобрёл телефон",
            "what is quantum computing",
            "history of the internet",
        ],
    },
    "dev": {
        "title": "Разработка",
        "primary_domain": "stackoverflow.com",
        "queries": [
            "how to reverse a list in python",
            "git undo last commit",
            "разница между процессом и потоком",
        ],
    },
}
