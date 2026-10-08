# Отдельный кабинет владельца SaaS

Самостоятельный control plane `app/saas_admin/`: существующий Supabase PostgreSQL,
приватная схема `restcontrol` и существующий Supabase Auth. Runtime не открывает SQLite,
не импортирует `app.portal`, настройки dashboard, планировщик и worker документов.
Публичный origin — `https://rc.chaika.team`. Наличие кода не подтверждает выпуск.

## Настройка и запуск

Установить `requirements-saas-admin.txt`. На сервере service читает приватный
`/opt/restcontrol-saas/runtime.env` (root:root, 0600) с четырьмя параметрами:

```text
CHAIKA_SAAS_DATABASE_URL=postgresql://restricted-role-connection
CHAIKA_SAAS_SUPABASE_URL=https://trusted-supabase-origin
CHAIKA_SAAS_ANON_KEY=<existing-anon-key>
CHAIKA_SAAS_AUTH_ADMIN_KEY=<existing-auth-admin-key>
```

Значения секретов задаются оператором вне исходников. Runtime connection должен
работать как `restcontrol_backend`, без superuser/BYPASSRLS и без доступа к таблицам
других продуктов. `auth_identities` предоставляет только UUID/email/маркер создания.
Auth admin key используется исключительно сервером, браузеру не передаётся.
Remote Auth URL требует HTTPS; HTTP допустим только для localhost/127.0.0.1.

Оператор применяет `supabase/migrations/20261008135638_restcontrol_supabase_registry.sql`
к существующему Supabase. Миграция добавочная: dashboard/auth пароли и их grants
не изменяются. Владелец — существующий Auth UUID с отдельным активным
`restcontrol.platform_memberships`; обычная учётная запись dashboard доступа SaaS
не получает. CLI `bootstrap` отклоняется: новый локальный пароль не создаётся.

В data-dir остаётся только исходный `credentials.key`: каталог 0700, ключ 0600.
Ключ не генерируется при запуске и не заменяется при потере. Перенос старого SQLite
выполняется отдельно через `app.saas_admin.import_registry`: ID компаний, история,
реквизиты и membership сохраняются; старые хеши паролей и сессии не импортируются.
Модули `repository.py`, `lifecycle.py`, `tenant_access.py` сохранены для миграции
и синтетических regression-тестов, не являются runtime или плановым backup.

```sh
python -m app.saas_admin serve --mode production --origin https://rc.chaika.team \
  --data-dir /opt/restcontrol-saas/data --dist-dir /release/frontend \
  --uds /opt/supabase/volumes/proxy/caddy/restcontrol-run/saas.sock
```

Frontend собирается отдельно: `npx vite build --mode saas-admin`, entry
`saas-admin.html` и `assets/`. Data/static не пересекаются. Unix socket только для
production, абсолютный путь вне data/static, несовместим с `--port`.
Без socket сервис слушает 127.0.0.1:8210. `--origin` в production обязателен:
canonical HTTPS DNS origin без port/path/query/userinfo. Перед стартом проверяются
ключ, роль PostgreSQL, активный platform membership и расшифровка connections.

Caddy завершает TLS и передаёт точные Host/Origin; Uvicorn не доверяет forwarded headers.
Cookie HttpOnly, SameSite=Strict, в production Secure и host-only, максимум 8 часов.
BFF хранит хеш cookie-токена и зашифрованные Auth access/refresh tokens в PostgreSQL;
каждый доступ проверяет Auth identity и отдельный SaaS membership. Изменения требуют
Origin и после входа X-CSRF-Token. Нет CORS или общих cookies dashboard.
Лимиты 10 неудач на peer/50 глобально за 15 минут хранятся в PostgreSQL.

## Резервная копия и восстановление

```sh
python -m app.saas_admin backup --data-dir /opt/restcontrol-saas/data \
  --output /opt/restcontrol-saas/backups/new-snapshot --clear-sessions
# Отдельное операторское окружение, никогда не runtime.env:
# CHAIKA_SAAS_RESTORE_DATABASE_URL=<operator-connection>
python -m app.saas_admin restore --input /private/backups/new-snapshot \
  --data-dir /private/recovery/new-key-directory
```

Backup берёт единый REPEATABLE READ READ ONLY снимок всех таблиц `restcontrol`,
метаданные колонок, количества, SHA-256 и согласованный ключ. Формат — приватные
`registry.json`, `manifest.json`, `credentials.key`; directory 0700/files 0600.
Сессии всегда исключаются (флаг `--clear-sessions` оставлен для совместимости).
Исходные данные/сессии не изменяются. SQLite и таблицы Auth не копируются.

Restore требует отдельный операторский DSN, новую директорию ключа, заранее
мигрированную точно совпадающую **пустую** схему. Все таблицы блокируются перед
проверкой пустоты. Runtime role restore выполнить не может. Восстановление строк
идёт в одной транзакции, сохранение ключа происходит до commit; ошибка откатывает
строки и удаляет новый каталог. Auth UUID должны уже существовать в целевом Auth;
пароли Auth не восстанавливаются и не изменяются. После восстановления нужен новый
вход. Restore в занятую рабочую схему запрещён, даже после остановки service.

Копия содержит ciphertext и ключ вместе: хранить целиком в закрытом storage и
сделать дополнительную приватную внешнюю копию. Для отката кода переключить
совместимый release, не восстанавливать старые бизнес-данные. Recovery требует
отдельной пустой схемы/базы и согласованного переключения DSN после проверки.

## API

Префикс `/api/saas-admin`. `GET /health` публичный: `{status:"ok",mode:"local"|"production"}`.
`POST /auth/login {username,password}` и `GET /auth/me` возвращают
`{user:{id,username,display_name},csrf_token}`. `POST /auth/logout` → 204.

Все компании и события доступны только вошедшему владельцу:

- `GET /companies?q=&status=&limit=50&offset=0` → `{items,total,limit,offset}`.
- `POST /companies` → новая карточка, 201.
- `GET /companies/{id}` → карточка.
- `PATCH /companies/{id}` → частичные поля + обязательный `expected_version`.
- `DELETE /companies/{id}?expected_version=N` → 204, **только архивирование**.
- `GET /companies/{id}/events?limit=50&offset=0` → история, включая архив.

Схема записываемых полей в `models.CompanyWrite`. Объекты modules/subscription/
primary_admin при PATCH заменяются целиком; остальные поля остаются прежними.
Ошибка: `{detail:{code,message,field?}}`, коды 401/403/404/409/422/429.
`expected_version` предотвращает потерю чужой правки. Уникальность slug и domain
сохраняется для архивных компаний. В обычной выдаче архивных записей нет.

Домены приводятся к lowercase IDNA без конечной точки, допускается HTTPS-префикс
и корневой `/`; пути, IP, локальные имена, credentials/port/query/fragment запрещены.
Адреса Chain/RMS — HTTP(S), без credentials/query/fragment внутри URL.
Сохранение не обращается к iiko. Отдельная кнопка проверки выполняет только вход/выход
по HTTPS:443, без загрузки бизнес-данных; контракт приведён ниже.
`primary_admin` — контакт, не новая учётная запись и не предоставление прав.
`status` задаётся вручную, не означает техническую готовность deployment.
Подписка — календарные даты, без денежных операций. Конечная дата включительна,
сегодня определяется в `Europe/Simferopol`. `integration_state` вычисляется по текущим
Chain и включённым RMS: `not_configured` без активных подключений,
`failed` при любой неуспешной проверке,
`not_checked` если хотя бы одно ещё не проверено, `ok` если все успешно проверены.
Отключённые RMS видны и допускают ручную проверку, но не влияют на общий статус.
Это результат последней ручной проверки, не мониторинг доступности; время — в connection.check.
Subscription state вычисляется при чтении.

## Хранение и проверки

Источник данных — PostgreSQL `restcontrol`; JSONB карточка и отдельные колонки
связаны CHECK constraints. Карточка, connections и audit фиксируются одной транзакцией.
Версия проверяется под блокировкой строки; уникальность slug/domain включает архив.
Audit append-only для runtime; platform memberships и migration manifest runtime
может только читать. Browser роли anon/authenticated не имеют доступа к схеме;
RLS ограничивает DB роль, API дополнительно проверяет компанию/роль пользователя.

Проверки: `python -m pytest tests/test_saas_admin*.py tests/test_saas_supabase_auth.py
 tests/test_saas_domain_boundary.py -q` и `ruff check app/saas_admin`.
PostgreSQL suite поднимает одноразовый PostgreSQL через pgserver; production/tenant
regression с явно переданным старым Repository проверяют HTTP-контракт без внешнего Auth.
Нативные тесты проверяют импорт, права, CRUD, rollback/audit, гонки, backup/restore,
отказ занятой схемы и отсутствующего Auth UUID. Настоящие Auth/browser входы на
целевом сервере требуют отдельной проверки после выпуска.

## Логины, пароли и ручная проверка Chain/RMS

`CompanyWrite` принимает необязательное write-only поле:

```json
{"connection_credentials":{"chain":{"login":"api-user","password":"new-secret"},
 "RMS-UUID":{"login":"api-user","password":"new-secret"}}}
```

Это поле никогда не возвращается внутри Company и не сохраняется в JSON карточки.
Новая компания с URL / новый RMS / изменённый URL требуют явных login+password.
При неизменных URL и login пароль можно не передавать — сохраняется прежний.
Изменённый login требует нового пароля. Пробелы пароля значимы и не обрезаются.
Карточка, зашифрованные реквизиты и audit сохраняются атомарно с expected_version.
Изменение URL/login/password сбрасывает прежнюю проверку; удаление подключения или
архив компании удаляет его зашифрованные реквизиты. Старые metadata-only карточки
читаются и допускают несвязанные правки без обязательного заполнения пароля.

GET `/companies/{id}/connections` → `{items:[{id,url,login,password_set,check}]}`,
где id — `chain` или UUID RMS. `check` содержит `{status,code,message,checked_at}`;
status — `not_checked`, `ok`, `failed`. Пароль никогда не возвращается.

POST `/companies/{id}/connections/{connection_id}/test` принимает `{expected_version}`,
использует сохранённый пароль и возвращает `{company_version,connection}`. Успешная
запись результата увеличивает company.version и создаёт audit без secret/error text.
Если настройки изменились во время проверки, результат отклоняется с 409; iiko-сессия
к этому времени уже освобождена или получено явное предупреждение о неудаче выхода.

POST `/connections/test` принимает `{url,login,password}` для несохранённого черновика.
Для прежнего пароля вместо password нужны `{company_id,connection_id,expected_version}`
и точно совпадающие URL/login. Возвращает объект check; **не сохраняет результат**.
После сохранения черновика отображается «Не проверено», пока пользователь отдельно
не проверит сохранённое подключение. На всех POST обязательны owner auth, Origin, CSRF.

Credentials хранятся Fernet-шифротекстом в отдельной таблице connections. Ключ
`credentials.key` — отдельный приватный файл 0600 в data-dir, никогда не в таблицах PostgreSQL.
Потеря ключа при существующем ciphertext закрывает запуск; новый ключ не подставляется.
Для восстановления нужны согласованные резервные копии БД и ключа; доступ к ним
следует разделять. Шифрование защищает отдельную копию БД, а не скомпрометированный хост.
В audit сохраняются только имена изменённых полей, не логины, пароли или токены.

Проверка: только `https://PUBLIC_HOST[:443]`, пути `/`, `/resto`, `/resto/api`;
GET `/resto/api/auth?login=…&pass=SHA1(password)`, затем GET `/resto/api/logout`
с Cookie `key=token` в finally. `ok` возможен только после подтверждённого выхода.
Неподтверждённый выход — отдельный `logout_failed`, не «неверный пароль».
Автоматического повторного входа после timeout/неопределённого результата нет.
Raw ответы, токены, URL auth-запросов и секреты не выводятся в логи/ответы API.

DNS проверяет весь набор A/AAAA: непубличные, multicast/reserved адреса запрещены.
TCP подключается к проверенному числовому IP, TLS проверяет исходное DNS-имя.
Повторного DNS при подключении, прокси из окружения и переходов по redirect нет.
DNS ждёт не более 2 с (пул и очередь ограничены двумя занятыми слотами), auth имеет
абсолютный бюджет 10 с, finally logout — отдельные 5 с. Watchdog закрывает сокет
в том числе при медленных TLS/HTTP-заголовках. Одновременно не более двух проверок;
не более 10 запросов владельца в минуту и одной проверки сервера в минуту, независимо
от вариантов пути root/resto/api. Проверки тестов синтетические; успешный реальный
вход может быть подтверждён только после явного ввода реквизитов пользователем.


### Проверка транспорта 08.10.2026

Исправлены две подтверждённые причины ошибочного «Сервер недоступен»: локальный
Python.framework не имел доступного CA bundle (stdlib `SSLCertVerificationError`,
verify_code=19), а законченный короткий HTTPResponse при `Connection: close`
закрывал сокет до следующего settimeout (OSError/EBADF, errno=9).
Теперь TLS использует явный Mozilla CA bundle из закреплённого certifi; проверка
сертификата и hostname остаётся обязательной. Чтение останавливается при завершении
HTTPResponse, а преждевременный EOF с остатком Content-Length отклоняется.
Ошибки безопасно различаются как `tls_failed`, `timeout`, `unreachable`;
сырые исключения и содержимое ответов не возвращаются пользователю.

После исправления только публичный `GET /` через тот же pinned HTTPS transport
прошёл на chayka-set-yalta-co.iiko.it (217.174.103.134),
chayka-na-plyazhe-ooo-more.iiko.it (194.67.114.5),
plove-ooo-briz.iiko.it (94.139.250.56): IPv4, HTTP 404, ответ 19 байт,
0,178–0,195 секунды. Это подтверждает TLS/чтение ответа, **не проверяет реквизиты**:
auth/logout в диагностике не выполнялись, сохранённые секреты не читались.
Синтетические regression-тесты используют настоящий stdlib HTTPResponse
через loopback HTTP: законченный короткий ответ, обрыв Content-Length,
включённая TLS-верификация и отдельные безопасные классы ошибок.

## Доступ администратора компании

Контакт `primary_admin` сохраняется независимо от создания доступа. Явное действие
владельца связывает существующего Auth пользователя по email, сохраняя его пароль.
При отсутствии email создаётся пользователь через Supabase Auth: случайный временный
пароль выдаётся один раз, действует 72 часа; до смены доступна только auth-группа.
Создание фиксирует marker до вызова Auth: при неопределённом результате повторное
создание запрещено до сверки. Импортированные memberships без Auth UUID имеют
статус `activation_required`. Отдельный tenant membership не даёт owner-доступ.

`GET /api/saas-admin/companies/{id}/admin-access` возвращает company_version,
exists, login_path, admin, can_reset_password=false. `POST` с expected_version
создаёт/активирует доступ. Reset общей identity запрещён с 409 shared_identity;
пароль существующей учётной записи меняет только её владелец.

Tenant API — `/api/saas-tenant/{slug}`: auth/login, auth/me, auth/logout,
auth/password и workspace. Отдельная host-only cookie `saas_tenant_session`
ограничена `/api/saas-tenant`. Смена пароля требует текущий пароль, Origin/CSRF,
отзывает tenant сессии администратора и создаёт новую. Supabase Auth хранит пароли,
BFF их хеши не сохраняет. Workspace содержит только компанию/модули и администратора,
`business_modules_ready:false`; реквизиты iiko/заметки/dashboard недоступны.
Suspended/archived запрещают вход; смена slug/приостановка/архив отзывает tenant sessions.

Общий URL — `https://rc.chaika.team/tenant/{slug}`. Настроенный company domain
сервер разрешает только через точное совпадение domain активной неархивной компании:
на таком host доступны context и tenant API/entry соответствующего slug; owner API
запрещён. Запись домена не подтверждает DNS/TLS или реальную browser проверку.

### Общий frontend в Apps и отдельный API origin

Для customer domain `iiko.tdpay.ru` общий статический frontend Apps использует
`https://api.iiko.tdpay.ru/api/saas-context` и свой
`/api/saas-tenant/{slug}/…`. API обслуживает тот же SaaS BFF; отдельный сервер
или база для клиента не создаётся. Конвенция для следующих компаний —
`api.<company.domain>`. Префикс `api.` зарезервирован для API: hostname после
удаления ровно одного префикса должен точно совпасть с domain в реестре.
Проверка реестра повторяется на каждом запросе и preflight; archived/suspended
или отключённый домен теряет доступ сразу. `X-Forwarded-Host` не выбирает компанию.

На API-host разрешены только context и API своего slug, без platform API,
чужого tenant или статического entry. Каждый запрос требует единственный точный
`Origin: https://<company.domain>`; hostname/Origin с портом, другой Origin,
несколько Origin и `Sec-Fetch-Site: cross-site` отклоняются. CORS разрешает
только этот проверенный origin, credentials и `Vary: Origin`; эти заголовки
добавляются также к ошибкам auth, validation и границы tenant для верного origin.
OPTIONS не требует сессии, но проверяет домен, origin и namespace; разрешены
только GET/POST и заголовки content-type/x-csrf-token. Прочие методы/заголовки
preflight получают 403. Вся прежняя проверка membership и CSRF для действий
остаётся обязательной.

Cookie сохраняет host-only, Secure, HttpOnly, SameSite=Strict и tenant path;
Domain не расширяется. HTTPS frontend/API должны быть same-site поддоменами,
чтобы браузер отправлял Strict cookie с `credentials: include`. Старый прямой
company domain и общий `rc.chaika.team/tenant/{slug}` продолжают поддерживаться
для перехода. Реальная работа требует отдельных DNS/TLS для frontend/API и
браузерной проверки; локальные тесты не подтверждают выпуск или DNS.
