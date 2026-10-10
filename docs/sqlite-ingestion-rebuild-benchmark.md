# Пересборка двух KG и benchmark SQLite ingestion

Дата: 11 октября 2026 (Europe/Helsinki). Результаты относятся к демо-набору
PostgreSQL 18 + pg_cron + PostGIS и Django + Patroni + `bridge_probe`,
**не к пользовательскому огромному Python-монорепозиторию**.

Финальный независимый read-only review: **GPT-6.1-Sol (XHigh), ACCEPT**.
Две неточности P3 (округление и заголовок размеров) исправлены;
нерешённых findings и blockers нет. Product code в этой задаче не менялся.

## Методика

Сравниваются baseline `050cfd6` и версия `f74e624` с выгрузкой CSV,
внешней стабильной сортировкой и файловым SQLite `.import`. Это сравнение
полного пакета изменений ingestion/layout/DDL, не отдельный эксперимент
«sort против executemany».

Все четыре экспорта выполнены последовательно: новая PG, новая Python,
baseline PG, baseline Python. Один прогон каждой комбинации, одинаковые
исходники, параметры и Docker image `codekg-dev:sqlite-stream`:
Python 3.12.13, SQLite CLI 3.46.1, GNU sort 9.7.
Baseline-код загружен из Git archive; путь импортируемого модуля проверен.
`projection_workers=2`, native extraction — serial file-at-a-time,
`native_fact_workers=1`, `bulk_extract_workers=1`.

**Хранилище экспорта — USB HDD**, TOSHIBA MQ04ABF100R (`/dev/sda1`,
`ROTA=1`), несмотря на название точки монтирования `MYSSD`.
Это исправляет прежнее предположение о SSD. Docker volumes и `/tmp`
на внутреннем SK Hynix NVMe. Во время экспорта новые Neo4j не запускались;
старые два Neo4j и MCP продолжали работать. Кеш ОС не сбрасывался,
порядок прогонов не рандомизирован; фоновые процессы и swap не исключены.
После экспортов чтение baseline заметно замедлилось, I/O PSI full avg60
достигала примерно 60%, `jbd2/sda1-8` находился в D-state. Последующая
проверка и candidate import конкурировали за этот же HDD. Эти наблюдения
не доказывают аппаратную неисправность и не включены во время экспорта;
изменение состояния storage/cache — дополнительный confound сравнения.
Это описательные результаты одной пары, не статистически подтверждённая
оценка для других машин или наборов данных.

Исходные upstream-ревизии (Git worktrees чистые):

| Snapshot | Commit |
| --- | --- |
| PostgreSQL REL_18_0 | `3d6a828938a5fa0444275d3d2f67b64ec3199eb7` |
| pg_cron | `5cedfa472ccc83567aa23ec645925ed8489a7797` |
| PostGIS | `8c02deb339a32ccf4a148ce2ada62480ce8d8c40` |
| Django | `cae7247962de54a94023d28d84f05fc9842e9646` |
| Patroni | `056e97bf32a4b0425156938231f9692ac2866fc9` |

`bridge_probe` — content-addressed синтетическая fixture, не upstream-проект.
Она содержит статически подтверждаемое создание psycopg connection/cursor
и три SQL-вызова: core, cron, PostGIS. Fixture не выполнялась против PG.

## Экспорт: wall time

Используется `manifest.metrics.elapsed_seconds` полного экспорта. Внешнее
`/usr/bin/time` Docker CLI дополнительно включает примерно 1–2 секунды
запуска. Это **не** время полного pipeline вместе с Neo4j import и индексом.

| KG | Baseline, с | Новая версия, с | Сокращение времени | Отношение baseline/new |
| --- | ---: | ---: | ---: | ---: |
| PG18 + расширения | 1619.411 | 494.121 | 69.49% | 3.28× |
| Python | 431.960 | 208.987 | 51.62% | 2.07× |
| Последовательная сумма | 2051.371 | 703.108 | 65.72% | 2.92× |

Фазы PG (с; их сумма не равна общему времени из-за прочих накладных затрат):

| Фаза | Baseline | Новая версия |
| --- | ---: | ---: |
| Native/source extraction | 300.141 | 186.819 |
| Bulk export | 37.411 | 31.213 |
| CSV composition | 117.131 | 63.521 |
| Identity | 18.673 | 11.300 |
| Resolution | 81.417 | 125.685 |
| Supplemental projection | 1051.504 | 54.473 |

Главная наблюдаемая разница — supplemental projection (примерно 19.3×).
В baseline этой фазы наблюдались ожидания journal commit и малая загрузка CPU
на HDD. **Resolution стала медленнее на 54.4%**; общий выигрыш этого не
отменяет, но требует отдельного повторяемого профилирования. Данных одного
прогона недостаточно, чтобы отделить эффекты сортировки, транзакций,
физического layout, файлового кеша и фоновой I/O.
Native/source extraction включает сопутствующие source-stage/spool операции,
а не только CPU-время native parser. Настройки durability serving SQLite
не отключались: результаты не получены ценой отказа от журнала durable corpus.

Фазы Python (с):

| Фаза | Baseline | Новая версия |
| --- | ---: | ---: |
| Native/source extraction | 200.298 | 93.742 |
| Bulk export | 107.439 | 81.576 |
| CSV composition | 24.617 | 19.652 |
| Identity | 7.912 | 10.162 |
| Resolution | 0.635 | 0.501 |
| Supplemental projection | 0.236 | 0.241 |

## Размер и память

| KG | Новый corpus SQLite, байт | Узлы | Связи | Parent peak RSS baseline/new, KiB | Max child RSS baseline/new, KiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| PG18 | 1638449152 | 809321 | 1629514 | 222108 / 202608 | 188580 / 202608 |
| Python | 30646272 | 249913 | 413676 | 123692 / 127536 | 123692 / 127536 |

RSS — отдельные максимумы процессов из manifest, **не сумма concurrent memory**.
RSS `/usr/bin/time docker ...` относится к Docker launcher, не extractor.
PG corpus около 1.53 GiB хранит подробные source/evidence факты, а не только
имена symbols. Сам по себе размер не объясняет длительность сборки.
Счётчики графа не включают отдельный bootstrap `CodeKGGeneration` marker.

## Проверка сохранности данных

Для **каждой из 12 таблиц каждой KG** новая версия и baseline совпали:
schema (`PRAGMA table_info`), число строк и SHA-256 мультимножества полных
строк. Каждая строка всех колонок сериализована в length-framed JSON,
хеширована, затем row hashes отсортированы GNU sort с ограничением 32 MiB;
дубликаты сохраняются. Порядок физических строк не учитывается. Совпали
также все graph counts и snapshot identities/native counts. Это
криптографическая semantic comparison, не побайтовое равенство SQLite файлов.

Готовые SQLite-файлы скопированы последовательно на внутренний NVMe **только
для проверки**, с последующим immutable read-only открытием. На exports
это не влияло. Проверка заняла 904.938 с, преимущественно в ожидании чтения
baseline HDD; это время не включено в таблицу export и не является временем
ingestion. Файлы корпусов не изменялись. Итог `compare-corpora.json`: `passed`.

## Новые Neo4j и MCP

Новые графы импортированы в отдельные volumes; один Community instance на KG.
Старые Neo4j (`17474`/`27474`) и MCP (`18765`) не изменялись. Candidate registry
не активирован вместо старого `config/active.toml`; новые графы обслуживает
отдельный MCP. Authentication включена; credentials находятся только
в permission-restricted env files, вне TOML и Git.

| Сервис | Container | Локальные адреса |
| --- | --- | --- |
| PG18 KG | `codekg-pg18-838f1a6d949f-6fc76c43` | Browser `http://127.0.0.1:38474/`; Bolt `bolt://127.0.0.1:38687` |
| Python KG | `codekg-python-ac41bd7711ca-5e698fc0` | Browser `http://127.0.0.1:48474/`; Bolt `bolt://127.0.0.1:48687` |
| Новый MCP | `codekg-rebuilt-mcp` | `http://127.0.0.1:18766/mcp` |

Generation IDs:

- PG: `pg18:838f1a6d949f4cfd318b8251eeeff67f`.
- Python: `python:ac41bd7711ca98cb80a4e2c960e797b8`.

Resources: Neo4j 5.26 Community; offline import memory 2 GiB, heap max 1 GiB,
page cache 512 MiB. Новые MCP и CLI загружают код `f74e624` через `PYTHONPATH`,
а не старый пакет из parent image. Binding локальный, не публичный.

## Prepare, bootstrap, lexical index

Время внешних CLI-команд включает Docker startup, проверку артефактов,
offline Neo4j import и запуск candidate. Это не чистое время neo4j-admin.

| Этап | PG18, с | Python, с |
| --- | ---: | ---: |
| `graph prepare` | 365.90 | 20.34 |
| `graph bootstrap` | 12.85 | 7.31 |

PG prepare конкурировал с проверкой baseline corpus за HDD. Эквивалентного
baseline Neo4j import не проводилось; эта таблица не доказывает ускорение
импорта. Heap/pagecache/import bounds приведены выше. Bootstrap проверил
per-label/per-relationship counts и записал точные generation markers;
итоговый combined `graph check --backend` прошёл. Ранняя попытка combined
check до запуска Python закономерно получила connection refused; после
запуска обоих графов проверка повторена успешно (`backend-check.log`).

Свежий Python lexical index в candidate generation: **33 285 документов**.
CLI metrics: 10.411 с (validation 0.568 + zvec 9.843), peak RSS 420428 KiB.
Внешний wall с Docker startup: 11.91 с. При этом index старого поколения
не используется. Анонимное подключение к обоим новым Neo4j отклонено
(`auth-check.json`); аутентифицированные backend checks прошли.

## MCP: correctness и latency

Успешно выполнены 11 correctness checks: 25 tools, обе точные generations
и шесть snapshot aliases; exact SQL resolution для core, cron и PostGIS;
extension visibility; отклонение stale reference; verified SQL → native
path; Python `EXACT_CALLS` chain с on-demand composition; обратное
использование core native target; lexical search реального Patroni symbol.
Мосты core/extension проверены на явно обозначенной fixture, не на исполнении
Django/Patroni против живого PostgreSQL.

Восемь сценариев, каждый: один first measured request и **20 sequential warm
samples** через FastMCP client/HTTP loopback. Проверки correctness идут до
замеров, поэтому first measured **не является cold request**. Сервер, ОС и
индекс уже прогреты, включая предварительные проверки/попытки benchmark.
Кеш не сбрасывался; throughput/concurrency/load и cold-start не измерялись.
Каждый измеренный ответ проверен семантически (exact resolution, непустой
search/path, ожидаемый reverse intent), а не только по отсутствию transport error.
Это client-observed round-trip latency, не CPU-время серверного tool.

| Сценарий | Warm p50, мс | Warm p95, мс |
| --- | ---: | ---: |
| `discovery` | 3.94 | 4.37 |
| `python_symbol_search` | 17.05 | 19.49 |
| `lexical_search` | 80.28 | 91.94 |
| `native_search` | 84.06 | 104.26 |
| `core_resolution` | 14.02 | 16.82 |
| `extension_resolution` | 14.16 | 15.97 |
| `forward_native` | 16.26 | 17.71 |
| `reverse_native` | 23.35 | 25.25 |

p50 — медиана; p95 — линейная интерполяция отсортированных 20 samples.
Все raw samples и first measured значения сохранены в `mcp-benchmark.json`;
итог `passed`. Малое число samples даёт только ориентир, не SLA или
статистическое сравнение с прежним MCP. Первый helper attempt использовал
неверное поле результата search (`name` вместо `qualified_name`); исправлен
только локальный benchmark helper, без изменений продукта. Затем helper
усилен проверкой каждого timed response и полностью повторён; предыдущие
attempt logs/results сохранены отдельно. В таблице только финальный прогон.

## Артефакты и воспроизводимость

Локальный artifact root (игнорируется Git):
`.codekg-corpus/federated-demo/rebuild-20261011-sort/`.

- `pg18/manifest.json`, `python/manifest.json` — новые экспорты.
- `baseline/pg18/manifest.json`, `baseline/python/manifest.json` — baseline.
- `reports/*-export.time`, `*-process.csv`, `*-docker-stats.csv` — wall и ресурсы.
- `reports/compare-corpora.py` / `.json` — semantic comparison.
- `reports/mcp-benchmark.py` / `.json` — live проверки и latency samples.
- `reports/source-status.json` — provenance. Для fixture Git status относится
  к родительскому checkout; единственный untracked `.project` не является
  изменением fixture и не включён в commit.

Полный DEBUG log новой PG потерян после удаления export-контейнера;
manifest, time и process/resource samples сохранены. Остальные export logs
сохранены. Большие corpus, CSV, resource logs и credentials не публикуются в Git.

Экспорты можно повторить по тому же operator recipe
[Independent graph operations](independent-graph-operations.md),
используя сохранённые локальные source configs:

```sh
codekg bulk-export-corpus .codekg-corpus/federated-demo/config/pg18.toml artifacts/pg18 --workers 2
codekg bulk-export-corpus .codekg-corpus/federated-demo/config/python.toml artifacts/python --workers 2
```

Для строгого повторного сравнения нужны несколько прогонов в чередующемся
порядке, одинаковая cache policy и отдельные stage/resource measurements.
Resolution следует профилировать отдельно, не скрывая её регрессию за total.
