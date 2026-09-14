# Домены сайта и API

| Адрес | Назначение |
| --- | --- |
| `https://dashboard.chaika.team` | Статический React-сайт из `team_front/dist` |
| `https://xx.chaika.team/api` | Пользовательский API `app.portal:app` |

Домены можно разместить на одном VPS или на разных. A/AAAA-записи каждого имени должны вести на соответствующий сервер. Supabase остаётся отдельным сервисом; его URL и ключ указываются в локальном `.env` backend.

Для Timeweb Cloud Apps используйте [отдельную инструкцию](timeweb.md). Конфигурации Caddy ниже предназначены для собственного VPS.

## Backend

Из корня `team_back` скопируйте `.env.example` в `.env`, если локального файла ещё нет. Заполните доступ к существующему Supabase и базе. В уже настроенном файле измените только:

```dotenv
CHAIKA_WEB_ORIGIN=https://dashboard.chaika.team
CHAIKA_WEB_SECURE_COOKIE=true
```

Образец остальных обязательных параметров — `ops/portal/production.env.example`. Это шаблон, он автоматически не загружается. Не заменяйте существующие рабочие значения пустыми значениями из шаблона.

Запустите портал под отдельным пользователем службы из каталога backend:

```sh
.venv/bin/python -m uvicorn app.portal:app --host 127.0.0.1 --port 8013 --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1 --no-access-log
```

Служба должна перезапускать этот процесс при сбое и при загрузке сервера. Caddy из этого примера работает на той же машине вне контейнера. При контейнерном размещении адрес upstream и доверенные proxy нужно настроить под контейнерную сеть.

Добавьте блок `ops/portal/Caddyfile` в конфигурацию Caddy на сервере backend. Он проксирует `/api/*` с `xx.chaika.team` на порт 8013, сохраняя префикс `/api`. Прочие адреса возвращают 404. Порт 8010 и `app.main` предназначены для внутренних запросов iiko и синхронизации, этот домен их не публикует. Caddy не должен удалять заголовки `Origin`, `Cookie` и `Set-Cookie`.

## Frontend

В `team_front` команда `npm run build` использует `.env.production`:

```dotenv
VITE_API_BASE_URL=https://xx.chaika.team/api
```

CI собирает такой же артефакт. Разместите `dist` на сервере сайта и добавьте `ops/Caddyfile` из frontend. Если используется другой веб-сервер, настройте fallback на `index.html` для маршрутов React и `connect-src 'self' https://xx.chaika.team` в CSP. Для отсутствующих файлов `/assets/*` сохраняйте 404.

Не переписывайте действующий Caddyfile других сайтов. Добавьте нужные блоки, проверьте и затем перезагрузите конфигурацию:

```sh
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Caddy получает HTTPS-сертификаты после правильной настройки DNS и доступности портов 80/443. Код и конфигурации в GitHub сами по себе не запускают приложение на сервере.

## Вход и доступ

CORS разрешает только точный адрес из `CHAIKA_WEB_ORIGIN`, методы GET/POST и заголовок Content-Type; отправка cookies разрешена. POST-запросы дополнительно проверяют Origin. Cookies остаются HttpOnly, Secure и SameSite=Strict, без общего Domain: оба HTTPS-домена относятся к одному сайту `chaika.team`, а cookie отправляется только API-хосту. Запросы frontend и обновление сессии используют `credentials: include`.

Текущий вход через email/пароль обращается к Supabase через backend. Для него не требуется добавлять frontend в CORS самого Supabase. Если позже появятся OAuth или ссылки восстановления пароля, их redirect URL нужно будет настроить отдельно.

## Проверка после запуска

```sh
curl -i -X OPTIONS https://xx.chaika.team/api/auth/login \
  -H 'Origin: https://dashboard.chaika.team' \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type'

curl -i https://xx.chaika.team/api/me \
  -H 'Origin: https://dashboard.chaika.team'
```

Первый запрос должен вернуть 200 с точным `Access-Control-Allow-Origin` и `Access-Control-Allow-Credentials: true`. Второй без сессии — 401 с CORS-заголовками. В браузере проверьте вход, обновление страницы, переход по прямой ссылке `/purchase-prices` и выход. Ответы API и HTML не кэшируются; файлы сборки с хешем кэшируются отдельно.

Для локального запуска оставьте `.env` с `http://127.0.0.1:8013` и `secure_cookie=false`, а frontend соберите `npm run build -- --mode development`. Режим `npm run dev` также использует локальный `/api` proxy.

## Документация протоколов

- [FastAPI: CORS](https://fastapi.tiangolo.com/tutorial/cors/)
- [Vite: переменные окружения и режимы](https://vite.dev/guide/env-and-mode)
- [MDN: Set-Cookie](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Set-Cookie)
- [Caddy: fallback через try_files](https://caddyserver.com/docs/caddyfile/directives/try_files)
