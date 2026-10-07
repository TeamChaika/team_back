# Запуск, публикация и диагностика

Проверено 2026-10-03 по коду, CI и Timeweb API. [Карта модулей](../PROJECT_MAP.md).

## Активные приложения

| Компонент | Репозиторий / ветка | Timeweb App | Публичный адрес |
| --- | --- | --- | --- |
| Frontend | `TeamChaika/team_front`, `chaikaiiko` | `254029`, FrontEnd Site | `https://dashboard.chaika.team` |
| Backend | `TeamChaika/team_back`, `chaikaiiko` | `254031`, Back Site | `https://xx.chaika.team/api` |

У обоих приложений включён автодеплой. Последние подтверждённые успешные версии при составлении карты: frontend `1cf12c3`, backend `e8ef0a1`. Это датированный снимок, не закреплённая версия для следующих выпусков.

## Процессы и точки входа

| Что | Где искать |
| --- | --- |
| Публичный HTTP-процесс | [app/serve.py](../../app/serve.py) → `app.portal:app`; `PORT`, по умолчанию 8000, один worker |
| Инициализация БД, Auth, фоновых задач | [app/portal.py](../../app/portal.py), `create_portal` / `lifespan` |
| Синхронизации по расписанию | [app/scheduler.py](../../app/scheduler.py), `JOBS`, `run_job`; включаются через `CHAIKA_SYNC_ENABLED` |
| Технический API iiko | [app/main.py](../../app/main.py); планировщик использует `127.0.0.1:8010`, не публиковать вместо портала |
| Документы, Telegram и справочники документов | [app/documents/worker.py](../../app/documents/worker.py); запуск `python -m app.documents.worker` |
| Контроль дочернего worker | [app/documents/runtime.py](../../app/documents/runtime.py); флаги `CHAIKA_WEB_DOCUMENTS_ENABLED`, `CHAIKA_DOCUMENTS_NATIVE_ENABLED`, `CHAIKA_DOCUMENTS_WORKER_ENABLED` |

Worker может работать отдельно от HTTP-приложения. При правках Telegram, отправки или восстановления очереди установить **фактическое место запуска и версию worker**: успешный деплой API не доказывает обновление отдельного процесса. Эта карта не подтверждает текущий хост worker. Не запускать второй старый polling-бот поверх нового.

## Конфигурация и схема

- Основные настройки: [app/core/config.py](../../app/core/config.py), web/cookies/CORS: [app/web/settings.py](../../app/web/settings.py), документы: [app/documents/config.py](../../app/documents/config.py).
- Образцы переменных: корневой `.env.example` и `.env.assistant.example`. Читать только необходимые параметры; не выводить токены, пароли и DSN в отчёты/карты.
- Аналитические миграции: [supabase/migrations](../../supabase/migrations); документы: [migrations/documents](../../migrations/documents). Production-миграции применяются отдельно от CI и автодеплоя.
- Не применять [tools/prepare_test_database.py](../../tools/prepare_test_database.py) к рабочей БД: он предназначен для пустой тестовой схемы на loopback.
- [ops/portal/Caddyfile](../../ops/portal/Caddyfile) — вариант для собственного reverse proxy, не доказательство использования Caddy в текущем Timeweb App.

## Проверки и выпуск

1. Проверить рабочую папку, remote, незакоммиченные изменения и актуальную `origin/chaikaiiko`.
2. Изменить только целевой модуль; выполнить связанные тесты. Backend: `python -m pytest …`, `python -m ruff check app tests tools main.py`. Frontend: `npm test`, `npm run build`.
3. [Backend CI](../../.github/workflows/ci.yml) использует изолированный PostgreSQL, Ruff, pytest и Docker smoke test. Не использовать рабочую БД как тестовую.
4. Публиковать через ветку `codex/...` и PR в `chaikaiiko`. Проверить CI именно для нужного SHA; не делать force push общей ветки.
5. После merge дождаться Timeweb deploy со статусом `success` и совпадающим SHA, затем проверить основной сценарий в браузере. HTTP 200 отдельно не доказывает работоспособность согласования или Telegram.
6. Для изменений схемы или отдельного worker проверить и эти части выпуска. Секреты, базы, рабочие выгрузки и `.local` в Git не добавлять.

## Симптом → первая проверка

| Симптом | Куда идти |
| --- | --- |
| `Could not import module main`, порт не найден | `app/serve.py`, [docs/timeweb.md](../timeweb.md), команда запуска и `PORT` текущего приложения |
| `PoolTimeout`, попытка подключения к локальному socket | Подключение БД из контейнера, имя нужной переменной; не увеличивать timeout вместо проверки адреса |
| `Failed to fetch`, вход не работает | `src/api.ts` frontend → Origin/CORS/cookies в `app/portal.py`, `app/web/settings.py` → Supabase Auth |
| Пустые / устаревшие показатели | Дата и scope запроса → источник `live_sales` или `indicators` → кэш/очередь → iiko; [расписания](../scheduled-sync.md) |
| Нет новых исторических данных | `app/scheduler.py`, статус сохранённых запусков, соответствующий `app/sync_*.py` |
| Согласовали, iiko не получил | `app/documents/dispatch.py`, статус задачи и heartbeat worker; `unknown` требует сверки, а не слепого повтора |
| Telegram не приходит / старое оформление | `app/documents/telegram.py`, версия реально работающего worker, права адресата, задачи уведомлений |
| Кнопка недоступна конкретному сотруднику | Раздел и активность профиля → складские actions или отдельные права pay; роль owner не заменяет эти проверки |
| Git обновлён, сайт прежний | SHA merge → SHA успешного Timeweb deploy → загруженный frontend asset / API; отдельно версия worker |

Подробные инструкции: [документы и очередь](../documents.md), [Timeweb](../timeweb.md), [домены](../domains.md), [расписания](../scheduled-sync.md), [агент Timeweb](../timeweb-ai.md).

## Коммерческие накладные

`app/commercial_invoices/` включается отдельно флагами `CHAIKA_DOCUMENTS_COMMERCIAL_ENABLED` и `CHAIKA_DOCUMENTS_COMMERCIAL_SUBMIT_ENABLED` у API и фактического worker. Нужны миграция documents/0008, закрытый `CHAIKA_DOCUMENTS_COMMERCIAL_SELLER_JSON`, свежие каталоги и явные складские права. [Контракт, состояния и проверка](../commercial-invoices.md). Отключение submit останавливает новые отправки; неизвестный исход разрешается только чтением и сверкой.
