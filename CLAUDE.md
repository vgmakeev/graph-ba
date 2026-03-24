# CLAUDE.md — Graph BA

## Что это

Graph BA — standalone CLI для графовой индексации и трассируемости артефактов бизнес-анализа. Работает с любым BA-проектом через конфигурацию `graph-ba.toml`.

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
| Семантический ревью | `graph-ba review F-01 --semantic --lines 30` |
| Аномалии графа | `graph-ba anomalies` |
| Матрица покрытия | `graph-ba coverage` |
| Кратчайший путь | `graph-ba path F-04 M09` |
| Impact analysis | `graph-ba impact BR.2` |
| BFS-обход | `graph-ba walk BP-03 --depth 2 --no-file` |
| Сироты | `graph-ba orphans --max-degree 1` |
| Хабы | `graph-ba hubs -n 10` |
| Кластер | `graph-ba cluster "кухня"` |
| SQL | `graph-ba sql "SELECT ..."` |
| Визуализация | `graph-ba render --no-file-nodes` |

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

## Архитектура

```
graph_ba/
├── config.py         — загрузка и валидация graph-ba.toml
├── traceability.py   — сканер артефактов, построение графа, экспорт
└── graph_db.py       — SQLite + FTS5 БД, CLI (click), anomaly detection
```

- `traceability.py` — ядро: скан определений, ссылок, построение NetworkX-графа, верификация, экспорт (JSON, DOT, HTML, ARTIFACT_INDEX.md)
- `graph_db.py` — импорт графа в SQLite, FTS5-поиск, CLI команды для навигации и анализа
- `config.py` — загрузка TOML конфига, нормализация ID, классификация

## Язык

Документация на русском. Общайся на русском, если пользователь пишет на русском.
