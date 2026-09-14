# Chaika Team — Backend

Python 3.12 + FastAPI: интеграция с iiko, сохранение истории в PostgreSQL/Supabase, API аналитического сайта и помощник по закупкам через OpenRouter.

Фронтенд React находится в отдельном репозитории `TeamChaika/team_front`. Для совместного запуска клонируйте оба репозитория в соседние папки.

## Состав

- `app/main.py` — технический API iiko и синхронизации.
- `app/portal.py` — API сайта, вход через Supabase Auth, доступ по ресторанам и раздача собранного фронтенда.
- `app/progress.py` — локальная страница прогресса загрузок.
- `app/sync_*.py` — загрузчики справочников, документов, OLAP и событий RMS.
- `supabase/migrations/` — схема и миграции PostgreSQL.
- `tests/`, `docs/`, `ops/` — проверки, описание интеграций и конфигурация инфраструктуры.

## Установка

Из корня этого репозитория:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

Заполните локальный `.env`: реквизиты iiko, `CHAIKA_DATABASE_URL`, `CHAIKA_SYNC_API_KEY`, параметры `CHAIKA_WEB_*`. Для помощника добавьте параметры из `.env.assistant.example`. Рабочие секреты и выгрузки данных в репозитории не хранятся.

## Домены production

- Сайт: `https://dashboard.chaika.team`
- API сайта: `https://xx.chaika.team/api`

[Настройка HTTPS, cookies и reverse proxy](docs/domains.md). Для этой схемы фронтенд раздаётся отдельно, а `xx` проксирует только API портала.

## Локальный запуск сайта

Сначала соберите соседний фронтенд:

```sh
npm --prefix ../team_front ci
npm --prefix ../team_front run build -- --mode development
```

В `.env` укажите `CHAIKA_WEB_FRONTEND_DIR=../team_front/dist`. Путь можно заменить абсолютным. Запуск из корня backend:

```sh
.venv/bin/python -m uvicorn app.portal:app --host 127.0.0.1 --port 8013 --no-access-log
```

Откройте http://127.0.0.1:8013/. Браузер обращается к `/api` на том же адресе. Для внешней публикации настройте HTTPS, точный `CHAIKA_WEB_ORIGIN` и `CHAIKA_WEB_SECURE_COOKIE=true`.

Технический API запускается отдельным процессом:

```sh
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8010 --workers 1 --no-access-log
```

Для iiko используйте один процесс без `--reload`, чтобы сохранять последовательность запросов и управлять сессией. Технический API оставляйте доступным только локально или в закрытой сети. Публичные запросы сайта обслуживает `app.portal`.

## Проверки

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check app tests tools
```

Без `CHAIKA_TEST_DATABASE_URL` проверки PostgreSQL пропускаются. В CI создаётся отдельный PostgreSQL 17 на `127.0.0.1:15438`, применяются все миграции, затем запускается полный набор тестов. `tools/prepare_test_database.py` предназначен только для новой пустой тестовой базы; он отказывается работать на другом адресе или в существующей схеме.

## CI/CD

`.github/workflows/ci.yml` запускает Ruff и pytest при push в `chaikaiiko`, при pull request и вручную. Результаты pytest сохраняются артефактом. Задание использует только временную тестовую базу и не подключается к рабочему Supabase/iiko.

Фронтенд имеет свой CI в отдельном репозитории. Автоматическое развёртывание пока не включено: после выбора сервера можно добавить отдельное CD-задание и GitHub Environment с параметрами доступа. Миграции рабочей базы не запускаются текущим CI.

## Документация

- [Архитектура и правила синхронизации](docs/architecture.md)
- [Frontend](https://github.com/TeamChaika/team_front)

Исходники и миграции публикуются отдельно от рабочей конфигурации. Внутренние протоколы загрузок, реальные отчёты, база данных, `.env` и `.local` остаются за пределами публичного репозитория.
