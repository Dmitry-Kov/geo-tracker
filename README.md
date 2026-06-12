# GEO-трекер Optimize.uz

**EN:** GEO (Generative Engine Optimization) tracker: monitors how often your
domains are cited in AI answers — Gemini (Google Search grounding), Perplexity
Sonar, OpenAI web search, plus a manual-checklist mode for engines without an
API (ChatGPT, Yandex Neuro). Official APIs only, no browser automation.
Results accumulate in an append-only CSV; reporting includes a console
summary, a self-contained HTML dashboard and a DOCX export. Put your domains
and queries into `queries.py` (copy it from `queries.example.py`), API keys
into `.env` — engines enable themselves based on which keys are present.

---

Мониторинг цитируемости ваших доменов в ответах ИИ: Gemini/Google AI,
Perplexity, ChatGPT, Яндекс Нейро.

Только официальные API — никакой браузерной автоматизации, нечему падать и ловить блоки.

## Установка

```bash
pip install requests                # для DOCX-экспорта дополнительно: pip install python-docx
cp .env.example .env                # впиши ключи
cp queries.example.py queries.py    # впиши свои домены, ниши и запросы
```

`queries.py` и `.env` — локальная конфигурация, в гит не попадают (`.gitignore`).

Минимальный бесплатный старт — один ключ Gemini: https://aistudio.google.com → Get API key
(без карты; free tier + бесплатная квота Google Search grounding покрывает наш объём).

## Использование

```bash
# Тестовый прогон: один движок, 2 запроса на нишу (~12 запросов)
python geo_tracker.py run --engines gemini --limit 2

# Полный прогон по всем движкам с ключами (~55 запросов на движок)
python geo_tracker.py run

# Дожать прогон после исчерпания квоты (пропускает запросы с готовым ответом за сегодня)
# Free tier Gemini: ~20 grounded-запросов в день, при исчерпании движок снимается с прогона сам
python geo_tracker.py run --resume

# Сводка в консоль + dashboard.html
python geo_tracker.py report
```

Результаты дописываются в `results.csv` построчно (история не перетирается,
при обрыве посреди прогона ничего не теряется). Каждый прогон — новая дата
в динамике на дашборде.

## Ручной режим (ChatGPT, Яндекс Нейро — у них нет API для цитат)

```bash
python geo_tracker.py manual-export --engine chatgpt
# → manual_chatgpt_ДАТА.csv: прогони запросы руками в обычном браузере,
#   впиши увиденные домены через запятую в колонки domains_in_sources / domains_in_text
python geo_tracker.py manual-import manual_chatgpt_ДАТА.csv
```

После импорта ручные данные попадают в общую статистику и на дашборд.

## Файлы

| Файл                 | Что это                                              |
|----------------------|------------------------------------------------------|
| `queries.example.py` | шаблон конфига — скопируй в `queries.py`             |
| `queries.py`         | твои домены, ниши, запросы (локальный, в гит не идёт)|
| `geo_tracker.py`     | CLI: run / report / manual-export / manual-import    |
| `report_html.py`     | генератор dashboard.html                             |
| `report_docx.py`     | экспорт отчёта в DOCX                                |
| `results.csv`        | вся история замеров (создаётся при первом прогоне)   |
| `dashboard.html`     | отчёт: матрица видимости, динамика, движки           |

## Стоимость (на момент июня 2026)

| Движок     | Сколько стоит наш объём (~55 запросов/прогон)               |
|------------|-------------------------------------------------------------|
| Gemini     | 0 сум — free tier, grounding в бесплатной месячной квоте    |
| Perplexity | $5/мес кредитов у Pro-подписки покрывают с запасом; иначе ~$0.5–1/прогон на sonar |
| OpenAI     | копейки на gpt-5-mini, но нужна карта                       |
| ChatGPT/Яндекс | бесплатно руками, ~15 минут в неделю                    |

## Автозапуск на сервере (Hetzner)

```bash
# еженедельный прогон по понедельникам в 06:00 + свежий дашборд
crontab -e
0 6 * * 1 cd /opt/geo-tracker && python3 geo_tracker.py run && python3 geo_tracker.py report
```

Дашборд можно отдать наружу любым способом (nginx, scp на ноут) — это один
самодостаточный HTML-файл (Chart.js грузится с CDN, нужен интернет при открытии).

## Как читать цифры

- **src** — домен среди источников/цитат ответа (сильный сигнал: ИИ опирается на нас);
- **txt** — домен упомянут в тексте ответа (ИИ нас рекомендует);
- **share of voice** — % запросов ниши, где случилось хотя бы одно из двух;
- кросс-цитирование видно сразу: в каждом ответе проверяются все домены, а не только целевой домен ниши.

Один и тот же запрос в разные дни даёт разные ответы (ИИ недетерминирован),
поэтому смотреть надо на тренд по нескольким прогонам, а не на единичный замер.
