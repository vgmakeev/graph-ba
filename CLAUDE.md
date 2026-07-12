# CLAUDE.md — Graph BA

## Что это

Graph BA — standalone CLI для графовой индексации и трассируемости артефактов бизнес-анализа. Работает с любым BA-проектом через конфигурацию `graph-ba.toml`.

## Основной рабочий цикл

Для обычной локальной правки canonical Markdown основной цикл короткий:
`правка owning artifacts` → `diff <TARGET>` → тесты/EVD. `diff` не требует
`CHG-*`: он сравнивает Git base и worktree, показывает stable-ID semantic/graph
delta, impact и scoped gaps до/после. Для новой capability, breaking change или
неоднозначного scope используется полный цикл: `change init` → правка owning
artifacts → `change ready` → human approval fingerprint →
implementation/evidence → повторный `change ready`. `ready` обновляет
отсутствующие provider projections, импортирует граф и одним запуском строит
semantic delta, impact, bounded graph, evidence plan, gate и worklist. `search`,
`path`, `sql`, `review` и другие команды — диагностика, а не обязательный
ритуал; `jq` в нормальном агентском цикле не нужен.

## AI-native SDLC contract

- Полная SQLite-модель хранит source, contract, proof, change и navigation слои.
- Gate/agent scope строится через `contract|delivery|navigation|full` projections,
  а не одним рекурсивным BFS.
- `MENTIONS` и `INDEX` никогда не являются acceptance proof.
- Код использует `IMPLEMENTS`, тесты/evidence — `VERIFIES`, UI — `RENDERS`.
  Происхождение ребра хранится в provider/file/line metadata, а не в отдельных
  relation ids.
- Enforced artifact-class matrix задаёт единственное направление между двумя
  разными классами. Reverse view использует incoming query. Исключения:
  same-class lifecycle и symmetric relations.
- Brownfield-документы не переписываются массово: typed связь добавляется
  стабильным `:::link`/`LNK-*` assertion в graph-native overlay.
- Core не навязывает онтологию типов. Проектные классы объявляют
  `capabilities` (`flow`, `decision`, `state`, `event`, `screen`, `acceptance`)
  и `required_proofs`. Старые `BP/BD/BR/...` должны переиспользоваться; новый
  тип вводится только для отсутствующей смысловой единицы с отдельным owner и
  lifecycle.
- `change ready` — основной one-shot UX: provider refresh, import, compile и
  единый итог Proposal/Approval/Delivery/Next. `change compile` — низкоуровневая
  генерация файлов. Не заставляй пользователя собирать workflow через jq.
- `diff <TARGET>` — основной review для правки без CHG: полный Git file delta,
  canonical stable-ID delta, typed graph delta и introduced/resolved/persistent
  gaps выбранного scope. Он объясняет Git diff, но не заменяет Git как историю.

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
| **Diff без CHG** | **`graph-ba diff F-01 --base-ref origin/main`** |
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
- **`capabilities` / `required_proofs`** — смысл класса и обязательные proof gates
- **`[behavior_model]`** — capability profile динамических целей
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

Скиллы лежат в `.claude/skills/` и автоактивируются Claude агентом. Все используют префикс `ba-`:

- **`/ba-reindex`** — переиндексация + аномалии
- **`/ba-review <ID>`** — семантический ревью артефакта
- **`/ba-find-anomalies`** — полный анализ аномалий графа
- **`/ba-audit`** — глобальный аудит: воронка аномалии → покрытие → семантический ревью подозрительных
- **`/ba-impact <ID>`** — каскадный анализ влияния: что затронет изменение артефакта

## Архитектура

```
graph_ba/
├── config.py         — загрузка и валидация graph-ba.toml
├── traceability.py   — сканер артефактов, построение графа, экспорт
├── db.py             — SQLite schema, import, FTS, NetworkX loader, file cache
├── review.py         — review/validate logic
├── lint.py           — content lint checks
├── audit.py          — coverage, anomalies, global audit logic
├── cli.py            — Click commands and text/JSON rendering
└── mcp_server.py     — MCP stdio server over the same run_* functions
tests/
├── conftest.py       — синтетический BA-проект (фикстуры)
├── test_config.py    — config loading, normalization, classification
├── test_scanning.py  — definition/reference scanning
├── test_graph.py     — graph construction, verification
├── test_db.py        — SQLite import, FTS, helpers
└── test_cli.py       — CLI commands + JSON output
```

- `traceability.py` — ядро: скан определений, ссылок, построение NetworkX-графа, верификация, экспорт (JSON, DOT, HTML, ARTIFACT_INDEX.md)
- `db.py` — импорт графа в SQLite, FTS5-поиск, schema version, scanned_files snapshot
- `cli.py` — CLI-команды, JSON/text рендеринг, auto-import guard
- `review.py`, `lint.py`, `audit.py` — чистые функции анализа, пригодные для CLI и MCP
- `config.py` — загрузка TOML конфига, нормализация ID, классификация

## MCP

Optional extra:

```bash
uv tool install --with mcp .
```

Claude Code:

```bash
claude mcp add graph-ba -- uvx --from ~/dev/graph-ba --with mcp graph-ba-mcp
```

Codex config example:

```toml
[mcp_servers.graph-ba]
command = "uvx"
args = ["--from", "~/dev/graph-ba", "--with", "mcp", "graph-ba-mcp"]
```

Tools: `ba_search`, `ba_node`, `ba_review`, `ba_impact`, `ba_diff`, `ba_coverage`, `ba_anomalies`, `ba_audit`, `ba_path`, `ba_sql`.

## Тесты

```bash
uv run pytest tests/ -v
```

Синтетический BA-проект в фикстурах: 5 типов артефактов, 11 определений, перекрёстные ссылки, dangling refs, coverage gaps. 122 теста покрывают все слои: config → scanning → graph → DB → CLI → audit.

## Язык

Документация на русском. Общайся на русском, если пользователь пишет на русском.
