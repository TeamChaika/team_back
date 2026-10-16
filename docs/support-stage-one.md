# Поддержка и сбор ошибок, этап 1

Код: `app/support/{models,store,capture,routes}.py`; PostgreSQL migration
`20261016170000_support_stage_one.sql`. Центральный сервер обслуживает собственную
SaaS-сессию и текущий platform membership. Основной dashboard использует собственную
обычную сессию. Подписка, готовность бизнес-модулей и права на склады не блокируют поддержку.

## API

Базы: `/api/support` основной dashboard; `/api/saas-tenant/{slug}/support` клиент;
`/api/saas-admin/support` владелец платформы. Клиентские POST требуют существующую
защиту Origin; tenant и owner также X-CSRF-Token. Компания и автор берутся на сервере
из проверенной сессии/фиксированной конфигурации, клиентские поля контекста запрещены.

- `GET /tickets?limit=25&offset=0` — свои обращения; owner видит все и имеет
  фильтры `company_id`, `status`. Ответ `{items,total,limit,offset}`, max limit 100.
- `POST /tickets` — `{client_request_id: UUID, description: string(10..4000),
  screen?: Screen, event_id?: UUID}`. UUID сохранять до подтверждённого ответа.
  Повтор идентичного запроса возвращает созданную запись; иной payload с тем же UUID → 409.
- `GET /tickets/{UUID}` — запись, `audit`, связанное `event|null`. Обычная сессия
  читает только собственные записи своей компании; роль company_admin это не расширяет.
- Owner `POST /tickets/{UUID}/status` — `{expected_revision,status,note?: string<=2000}`;
  stale revision → 409. Статусы `new`, `investigating`, `resolved`, `dismissed`;
  допускается повторное открытие. Audit append-only, содержит автора, переход, время, ревизию.
- Клиент `POST /events` — `{client_request_id:UUID,source:'browser',
  code:'browser_error'|'unhandled_rejection'|'api_error',screen?:Screen,
  status_code?:400..599,request_id?:UUID}` → `{id:UUID}`.
- Owner `GET /events?limit&offset&company_id`, `GET /events/{UUID}`.

Screen: `overview,sales,reports,documents,transfers,writeoffs,incoming,outgoing,
deposits,management,profile,other`. Неизвестная страница определяется frontend как other.
Все записи имеют `company_id,company_name,actor_id,actor_name`; ticket дополнительно
`description,screen,status,revision,created_at,updated_at,event_id`; event —
`source,code,screen,status_code,request_id,diagnostic,occurrences,first_seen,last_seen`.
Audit: `id,actor_id,actor_name,from_status,to_status,note,created_at,revision`.

## Данные и граница доступа

Новая схема `support` в существующем Supabase PostgreSQL; не включать её в exposed
PostgREST schemas. `anon`/`authenticated` не имеют доступа. Central
`restcontrol_backend` получает доступ только к новой схеме; существующие business
роли и бизнес-данные не меняются. `support.principals` доступна только оператору.
SECURITY DEFINER `can_access` связывает PostgreSQL **session_user**, а не GUC или
переданное company_id, с одной компанией либо центральным backend; FORCE RLS
действует на всех таблицах. Центральная API-авторизация отдельно ограничивает пользователя.

Основной portal использует **отдельный** login `chaika_support`, с grant-командами
в конце миграции. Нельзя передавать main процессу DSN `restcontrol_backend`.
При настройке:

```
SUPPORT_DATABASE_URL=<private DSN of chaika_support>
SUPPORT_COMPANY_ID=primary-dashboard
SUPPORT_COMPANY_NAME=Чайка
```

Пароль генерируется вне Git; principal row фиксирует `primary-dashboard` независимо
от env. Central по умолчанию использует собственный registry DSN. Tenant children
не получают central/support credentials; их HTTP 5xx собирает gateway, фоновые
ошибки — центральный observer через уже изолированный tenant runtime.

Автоматические события не содержат URL, query, headers, body, cookie, access token,
текст исключения, locals, исходные строки кода или произвольные метаданные.
Сервер сохраняет только тип исключения и до 8 кадров module/function/line внутри
собственного `app/`; чужие filesystem пути отбрасываются. Браузерный collector не
отправляет message/stack. Описание пользователя и заметка оператора — сознательный
ввод; HTTP-ссылки, относительные пути, credential assignments и длинные ASCII
идентификаторы редактируются до записи. Это не обещание автоматического распознавания
любого секрета в свободном тексте.

## Надёжность, пределы и обслуживание

DB idempotency и блокировка ключа защищают конкурентные повторы. Лимиты в БД:
5 новых обращений и 30 новых событий на пользователя компании в минуту; ответы 429
с Retry-After 60. События одинакового company/actor/source/code/screen/status/diagnostic
группируются в часовое окно с occurrences. HTTP возвращает новый UUID X-Request-ID.

Автоматический capture использует bounded queue 128 и отдельный daemon thread;
сетевой connect timeout 3с, statement 3с, lock 1.5с. Переполненная очередь теряет
события; отказ поддержки не задерживает и не ломает бизнес-операцию. Shutdown
пытается освободить очередь до 1с. Это best-effort диагностика, не гарантированный
журнал транзакций. Саму ошибку поддержки повторно не собираем. Создание обращения
успешно только после PostgreSQL commit; при 503 пользователь сохраняет UUID и текст.

Central observer вызывает `SupportStore.prune()` раз в час: события и receipts 30 дней,
rate buckets 1 день; закрытые обращения resolved/dismissed с audit — 365 дней
после последнего изменения. Новые и находящиеся в работе обращения не удаляются. Один вызов
удаляет не более 1000 строк каждого основного типа. При задержке обслуживания старые
данные удаляются последующими пакетами. Ручной запуск тем же центральным principal:
`SUPPORT_DATABASE_URL=<central DSN> python -m app.support`. Не запускать от primary role.
Никаких автоматических исправлений, отправки внешних сообщений и повторов документов.

## Проверки и включение

1. Применить additive migration администратором PostgreSQL после schema backup.
2. Создать отдельный primary login, mapping и ровно указанные в SQL grants.
3. Опубликовать backend, задать private main env; shared frontend отдельно.
4. Проверить реальные сессии main, tenant, owner: создание → своя история → owner
   карточка → смена статуса; запреты чужого tenant/user, CSRF, RLS; безопасное событие
   тестовой ошибки без реальных операций с оплатой/документами.
5. Проверить observer, очистку и резервное копирование схемы support.

Локальные тесты `tests/test_support_stage_one.py`; PostgreSQL тесты требуют
`SUPPORT_TEST_DSN` выделенной **локальной тестовой** БД с миграцией и test login
`restcontrol_backend`, `chaika_support→primary-dashboard`, `other_support→other-company`.
Нельзя запускать fixture против production. Подробности выпуска — отдельная release запись.
Откат кода сохраняет данные; таблицы не удалять при rollback. Старый код поддержку не показывает.

CI создаёт отдельные restricted test login с SCRAM-паролем из disposable DSN до
миграций (`tools/prepare_test_database.py`), затем company mappings и ограниченные
grants поддержки. Workflow явно передаёт `SUPPORT_TEST_DSN`; тесты также принимают
`CHAIKA_TEST_DATABASE_URL`. Primary тестирует собственную поверхность, SaaS дополнительно
проверяет tenant CSRF/slug и owner access. Эти роли создаются только в тестовом bootstrap,
production миграция не создаёт роли и не изменяет пароль существующего registry principal.
