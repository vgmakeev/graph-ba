# CLAUDE.md — Graph BA

## Что это

Graph BA — standalone CLI для графовой индексации и трассируемости артефактов бизнес-анализа. Работает с любым BA-проектом через конфигурацию `graph-ba.toml`.

## Основной рабочий цикл

Два ключевых действия — суть проекта:

1. **`graph-ba import`** — переиндексация: скан markdown-файлов, построение графа в SQLite
2. **`graph-ba review <ID> --semantic`** — семантический ревью: собирает полный текст всех связанных артефактов и проверяет полноту, непротиворечивость, трассируемость

Всё остальное (search, anomalies, coverage, path, impact) — вспомогательные инструменты навигации по графу.

## Установка и запуск

```bash
# Из директории BA-проекта (где лежит graph-ba.toml):
uvx --from ~/dev/graph-ba graph-ba --help

# Или через uv run:
uv run --with ~/dev/graph-ba graph-ba import
```

## Ключевые команды

| Задача | Команда |
|---|---|
| Создать конфиг | `graph-ba init` |
| Переиндексировать | `graph-ba import` |
| Поиск по теме | `graph-ba search "тема"` |
| Детали артефакта | `graph-ba node BP-03` |
| **Семантический ревью** | **`graph-ba review F-01 --semantic --lines 20`** |
| Аномалии графа | `graph-ba anomalies` |
| Матрица покрытия | `graph-ba coverage` |
| Кратчайший путь | `graph-ba path F-04 M09` |
| Impact analysis | `graph-ba impact BR.2` |
| **Глобальный аудит** | **`graph-ba audit`** |
| SQL | `graph-ba sql "SELECT ..."` |

## Конфигурация (graph-ba.toml)

Файл `graph-ba.toml` размещается в корне BA-проекта и определяет:

- **`[scan]`** — директории для сканирования .md файлов
- **`[types.*]`** — типы артефактов с regex-паттернами для ID
- **`[[definitions]]`** — правила поиска определений (heading/table, поддерживает glob)
- **`[[index_tables]]`** — индексные таблицы для извлечения перекрёстных ссылок
- **`[[coverage]]`** — ожидаемые межслойные связи для матрицы покрытия
- **`[review]`** — валидационные правила (обязательные секции, двусторонние ссылки)
- **`[clusters]`** — семантические кластеры (тема → список ID)
- **`[normalize]`** — правила нормализации ID (замена символов, zero-padding)

## JSON-вывод

Глобальный флаг `--json` переключает вывод всех команд в JSON:

```bash
graph-ba --json search "тема"
graph-ba --json node F-01
graph-ba --json anomalies
graph-ba --json coverage
```

## Скиллы для Claude Code

Скиллы лежат в `.claude/skills/` и автоактивируются Claude агентом:

- **`/reindex`** — переиндексация + аномалии
- **`/review <ID>`** — семантический ревью артефакта
- **`/find-anomalies`** — полный анализ аномалий графа
- **`/audit`** — глобальный аудит: воронка аномалии → покрытие → семантический ревью подозрительных

## Архитектура

```
graph_ba/
├── config.py         — загрузка и валидация graph-ba.toml
├── traceability.py   — сканер артефактов, построение графа, экспорт
└── graph_db.py       — SQLite + FTS5 БД, CLI (click), anomaly detection
tests/
├── conftest.py       — синтетический BA-проект (фикстуры)
├── test_config.py    — config loading, normalization, classification
├── test_scanning.py  — definition/reference scanning
├── test_graph.py     — graph construction, verification
├── test_db.py        — SQLite import, FTS, helpers
└── test_cli.py       — CLI commands + JSON output
```

- `traceability.py` — ядро: скан определений, ссылок, построение NetworkX-графа, верификация, экспорт (JSON, DOT, HTML, ARTIFACT_INDEX.md)
- `graph_db.py` — импорт графа в SQLite, FTS5-поиск, CLI команды для навигации и анализа
- `config.py` — загрузка TOML конфига, нормализация ID, классификация

## Тесты

```bash
uv run pytest tests/ -v
```

Синтетический BA-проект в фикстурах: 5 типов артефактов, 11 определений, перекрёстные ссылки, dangling refs, coverage gaps. 122 теста покрывают все слои: config → scanning → graph → DB → CLI → audit.

## Язык

Документация на русском. Общайся на русском, если пользователь пишет на русском.
