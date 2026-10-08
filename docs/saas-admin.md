# Отдельный кабинет владельца SaaS

Первый самостоятельный реестр компаний, код `app/saas_admin/`. Не импортирует
`app.portal`, web-auth, планировщик, настройки tenant PostgreSQL и iiko.
Публичный origin — `https://rc.chaika.team`; DNS, TLS и постоянный диск настраиваются отдельно.
Сам код production-режима не подтверждает состоявшийся выпуск.

## Запуск

Из корня backend, в окружении с FastAPI, Pydantic 2, Uvicorn, cryptography 48.0.1 и certifi 2026.7.22:

```sh
python -m app.saas_admin bootstrap --data-dir /private/path/registry \
  --username owner --display-name 'Владелец' --password-file /private/path/password.txt
python -m app.saas_admin serve --data-dir /private/path/registry \
  --dist-dir /path/to/frontend/dist-saas-admin --port 8210
```

Перед запуском собрать отдельный интерфейс из каталога frontend: `npx vite build --mode saas-admin`.
Обычная сборка dashboard в `dist` не содержит `saas-admin.html`.

Файл пароля должен иметь права 0600; минимум 14 символов. Bootstrap одноразовый,
пароль не печатается и не передаётся аргументом процесса. После bootstrap удалить
исходный парольный файл после безопасного сохранения пароля владельцем.
SQLite содержит scrypt с индивидуальной солью; сессии — только SHA-256 случайного
256-битного токена. Cookie HttpOnly, SameSite=Strict, срок 8 часов, отдельный API-path.
В local Secure отсутствует для loopback HTTP; в production обе cookie имеют Secure,
включая удаление, и не задают Domain (host-only).

В local вход через `http://127.0.0.1:8210`, в production — через настроенный HTTPS origin.
Host должен точно совпадать. Изменения,
включая login, требуют точного Origin; после входа изменения требуют X-CSRF-Token.
Нет доверия X-Forwarded-For, CORS, общих cookies или пользователей рабочего сайта.
Login ограничен 10 неудачами на peer и 50 глобально за 15 минут; лимиты сохраняются
в отдельной БД. Ответы входа не раскрывают наличие пользователя.

## Production на постоянном VPS

Отдельный процесс, один экземпляр, SQLite на постоянном локальном диске. Не размещать
реестр в контейнерном слое Timeweb Apps или на сетевом filesystem. Не импортировать
`app.portal`, не добавлять пользователей SaaS в Chaika. Business modules пока не подключены.

```sh
python -m app.saas_admin serve --mode production --origin https://rc.chaika.team \
  --data-dir /private/persistent/registry --dist-dir /release/dist-saas-admin \
  --uds /private/run/saas.sock
```

`--uds` принимает абсолютный путь вне data/dist, только production, несовместим с `--port`.
Без него bind всегда `127.0.0.1`, port по умолчанию 8210. Сервис работает непривилегированным
пользователем; владелец инфраструктуры даёт Caddy доступ к каталогу/socket и проверяет его
после restart. Caddy завершает TLS, передаёт исходный точный Host и Origin, проксирует
SPA и API одним origin. Публичного backend-порта нет, CORS не включается. Uvicorn
`proxy_headers=False`: forwarded Host/IP игнорируются. Per-peer лимит за proxy общий;
глобальные лимиты владельца и tenant сохраняются. `--origin` обязателен, canonical HTTPS
DNS hostname без port/path/query/userinfo. Запросы с другим/дублированным Host или Origin
отклоняются; Origin проверяется у изменений, CSRF дополнительно после входа.

Production до открытия Repository проверяет существующий data-dir 0700, БД/ключ 0600,
схему 3, целостность/FK, владельца и расшифровку всех ciphertext. Нет автоматического
пустого bootstrap при потере диска/ключа. Обязательны собранные saas-admin.html и assets.
Ответы содержат HSTS; health и tenant workspace показывают `mode:"production"`.

### Снимок, перенос и восстановление

```sh
python -m app.saas_admin backup --data-dir /private/local/registry \
  --output /private/backups/new-snapshot --clear-sessions
python -m app.saas_admin restore --input /private/backups/new-snapshot \
  --data-dir /private/persistent/new-registry
```

Оба целевых каталога должны отсутствовать; существующие никогда не заменяются.
SQLite backup API создаёт согласованный снимок с WAL, рядом сохраняется оригинальный
credentials.key. Проверяются integrity/FK/схема/owner и расшифровка, секреты не печатаются.
Backup без `--clear-sessions` сохраняет сессии; export с флагом и любой restore удаляют
owner/tenant sessions только из копии. Компании, ID, аудит, пользователи, password hashes,
зашифрованные реквизиты и ключ сохраняются; локальный источник не изменяется.

Снимки приватны и содержат ключ вместе с БД: хранить их в закрытом backup storage,
отдельно от публичной сборки, предусмотреть защищённую внешнюю копию. Для отката остановить
процесс, восстановить в новый каталог, проверить соответствие версии кода схеме, переключить
путь и запустить один экземпляр. Проверить вход и сохранность данных после restart/redeploy.
Не копировать работающий registry.sqlite3 обычным cp, не совмещать два writable экземпляра.


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

Пустой реестр по умолчанию, без seed/demo. Единственный источник — закрытый data-dir
(0700) с registry.sqlite3 (0600); CLI устанавливает umask 077. FK, WAL, busy_timeout,
user_version=3 (добавочные миграции со схем 1/2 сохраняют карточки/историю). Изменение карточки и audit фиксируются в одной транзакции BEGIN
IMMEDIATE. История содержит автора, время, действие, названия изменённых полей;
пароли и значения полей в audit не копируются. Сессии и лимиты также переживают restart.

Отдельный SPA entry — `saas-admin.html`, assets из `assets/`; неизвестный asset
возвращает 404, navigation route — этот entry, неизвестный API не получает HTML.
Разрешённый resolved-путь asset ограничен подкаталогом assets, а SPA entry — dist.
Пересечение data-dir/dist, включая symlink на data-dir, запрещает запуск до создания БД.
Поиск учитывает название без регистра (включая кириллицу), slug и нормализованный домен.

Проверки: `python -m pytest tests/test_saas_admin*.py -q` и
`ruff check app/saas_admin tests/test_saas_admin*.py`. Проверяются вход/CSRF/Host,
валидация, IDNA, конфликты версий и уникальности включая архив, сохранность данных,
атомарный rollback при отказе audit, гонка обновлений, throttling, production prerequisites.
`tests/test_saas_admin_production.py`: HTTPS/Host/Origin/CSRF, Secure cookie владельца
и tenant при входе/смене/выходе, потерянный/неверный ключ, снимок/restore с сохранением
данных и отзывом только копируемых сессий; синтетические данные, без запросов в iiko.


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
`credentials.key` — отдельный приватный файл 0600 в data-dir, никогда не в SQLite.
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

## Доступ администратора компании (схема 3)

Явное создание доступа владельцем — отдельное действие после сохранения контакта.
Сохранение `primary_admin` никогда не создаёт пользователя. Один администратор на
компанию: email (без учёта регистра) и имя копируются при создании; последующие
изменения контакта и сброс пароля не переименовывают учётную запись.

- `GET /api/saas-admin/companies/{id}/admin-access` →
  `{company_version,exists,login_path,admin}`. `admin` равен null либо содержит
  `{id,username,display_name,must_change_password,temporary_expires_at,status}`.
  Статусы: `temporary`, `expired`, `active`, `blocked`.
- `POST` по тому же пути с `{expected_version}` создаёт доступ (201).
- `POST .../admin-access/reset` с `{expected_version}` заменяет пароль и отзывает
  все сессии этого администратора. UI требует подтверждения сброса.
- Обе операции увеличивают версию компании, атомарно записывают audit и возвращают
  дополнительно `temporary_password` только в этом ответе, с `Cache-Control: no-store`.
  Пароль генерируется случайно (192 бита), хранится только как scrypt с солью,
  действует 72 часа. GET, аудит и логи его не содержат. Повторно прочитать нельзя;
  после потери ответа требуется явный сброс с актуальной версией.

Адрес — `/tenant/{slug}` на том же origin: loopback в local,
`https://rc.chaika.team/tenant/{slug}` в production.
Настроенный домен компании остаётся метаданными будущего развёртывания.
Это отдельный кабинет компании; рабочие бизнес-модули ещё не подключены.
Владелец не получает tenant-сессию автоматически и tenant не получает owner-сессию.

Tenant API имеет префикс `/api/saas-tenant/{slug}`:

- `POST /auth/login {username,password}` и `GET /auth/me` →
  `{user:{id,username,display_name,company_id},company:{id,name,slug},must_change_password,csrf_token}`.
- `POST /auth/password {current_password,new_password}` → тот же объект и новая
  cookie/CSRF; новый пароль минимум 8 символов, отличается от текущего. Все старые
  сессии отзываются. До смены доступна только auth-группа, workspace возвращает
  `403 password_change_required`. Срок временного пароля проверяется и в сессии.
- `POST /auth/logout` → 204.
- `GET /workspace` → `{company:{id,name,slug,modules},admin:{id,username,display_name},
  mode:"local"|"production",business_modules_ready:false}`. Ответ не содержит реквизитов iiko,
  заметок, контактов, чужих компаний или бизнес-данных существующего dashboard.

Отдельная HttpOnly/SameSite=Strict cookie `saas_tenant_session` ограничена путём
`/api/saas-tenant`; токены хранятся только как SHA-256. Сессии до 8 часов (временные
не дольше срока пароля). Company ID определяется сервером из сессии; slug должен
соответствовать её компании. Изменения требуют Origin и X-CSRF-Token, login — Origin.
Проверки Host/размера запросов общие. Login и неверный текущий пароль ограничены
10 ошибками на peer / 50 глобально за 15 минут, отдельно от лимитов владельца.
Draft допускает вход; suspended/archived запрещают его. При приостановке,
архивировании или смене slug все tenant-сессии удаляются в транзакции изменения.
Возобновление компании не восстанавливает отозванные сессии.

Миграция 2→3 добавляет `tenant_admins`, `tenant_sessions`, `tenant_events` внутри
транзакции; старые компании, история и зашифрованные подключения сохраняются.
Перед обновлением действующего локального реестра сохранить согласованную SQLite
backup и `credentials.key` в приватном каталоге. Отдельный tenant audit содержит
только действие смены пароля, ID администратора/компании и время, без секретов.
Синтетические тесты проверяют миграцию, конкурирующее создание, rollback audit,
изоляцию компаний/owner, обязательную смену, ротацию, сброс, срок и блокировку.
