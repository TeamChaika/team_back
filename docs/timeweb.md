# Запуск backend в Timeweb Cloud Apps

API сайта находится в `app.portal:app`. Технический `app.main:app` обслуживает интеграцию iiko и не предназначен для публикации на `xx.chaika.team`.

## Существующее приложение с типом Python: FastAPI

Обновите приложение из ветки `chaikaiiko` репозитория `TeamChaika/team_back` и задайте:

| Настройка | Значение |
| --- | --- |
| Версия Python | 3.12 |
| Директория проекта | Корень репозитория |
| Команда сборки | `python -m pip install -r requirements.txt` |
| Команда запуска | `python -m app.serve` |
| Переменная `PORT` | `8000` |
| Путь проверки состояния, если доступен | `/api/health` |

Этот запуск читает `PORT` непосредственно в Python, поэтому не зависит от раскрытия `${PORT:-8000}` оболочкой. При отсутствии переменной используется 8000. Хост — `0.0.0.0`, один процесс, access log отключён, ошибки запуска идут в журнал приложения. Если на платформе настроен другой HTTP-порт, значение `PORT` должно ему соответствовать.

Добавлен также корневой `main.py`: стандартная команда пресета `uvicorn main:app --host 0.0.0.0` теперь импортирует публичный портал. Для явного управления портом используйте команду из таблицы. `uvicorn==0.52.4` уже зафиксирован в `requirements.txt`; замена предустановленной платформой версии при установке зависимостей ожидаема.

## Запуск через Dockerfile

В корне есть `Dockerfile` и `.dockerignore`. Тип приложения — Dockerfile, директория проекта — корень репозитория, ветка — `chaikaiiko`. Команда запуска уже указана в Dockerfile; переопределять её не требуется. Образ объявляет `EXPOSE 8000` и запускает `python -m app.serve`, переменная `PORT` по умолчанию также 8000. Не меняйте `PORT`, оставляя маршрутизацию Timeweb на 8000.

Настройте проверку состояния `/api/health`. TLS и домен `xx.chaika.team` обслуживает платформа. Caddy из инструкции для собственного VPS в этот контейнер не устанавливается.

### Зависимости Docker-сборки

`requirements.txt` задаёт прежние 12 прямых зависимостей. Docker устанавливает
полный набор из `requirements-linux.lock`: зафиксированы также транзитивные
версии и SHA256 конкретных wheels для Linux amd64 / CPython 3.12. Lock не
предназначен для установки нативных пакетов macOS/ARM; образ на другом хосте
собирайте с `docker build --platform=linux/amd64`.

После сбоя 18.10.2026 (PyPI wheels 21–25 kB/s и лимит сборки 1680 секунд)
Docker использует HTTPS index Tsinghua, документированный его владельцем:
[PyPI mirror](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/).
Это только транспорт: каждый разрешённый SHA взят из официального
[PyPI JSON API](https://docs.pypi.org/api/json/), а не из зеркала.
`--require-hashes --only-binary=:all:` запрещает незакреплённые зависимости,
изменённые wheels и сборку из исходных архивов. При недоступном или неполном
зеркале сборка завершается ошибкой; запасной index и отключение TLS не включены.
Ключи приложения, БД и S3 для установки пакетов не нужны.

При изменении `requirements.txt` пересоберите весь lock: разрешите зависимости
для CPython 3.12 с Linux amd64 wheels через официальный `https://pypi.org/simple`,
сверьте каждый файл, размер и SHA256 с
`https://pypi.org/pypi/<name>/<version>/json`, затем зафиксируйте все версии
и hashes. Сохраните extras, например `psycopg[binary]`.
Проверьте установку из зеркала с новым lock и `pip check`; перед выпуском
дождитесь Docker build и smoke test в CI. Wheels в Git не добавляются.
Не менять lock вместе с транспортом только ради получения более новых пакетов.

## Переменные приложения

В панели Timeweb сохраните рабочие значения:

- `CHAIKA_DATABASE_URL` — PostgreSQL/Supabase, доступный **из контейнера**.
- `CHAIKA_WEB_SUPABASE_URL` — URL рабочего Supabase.
- `CHAIKA_WEB_ANON_KEY` — его anon/publishable-ключ.
- `CHAIKA_WEB_ORIGIN=https://dashboard.chaika.team`.
- `CHAIKA_WEB_SECURE_COOKIE=true`.

Адрес `127.0.0.1` в `CHAIKA_DATABASE_URL` внутри контейнера указывает на сам контейнер. Локальный SSH-туннель на компьютере разработчика платформе недоступен. Нужен отдельно настроенный сетевой путь к базе; рабочие реквизиты не добавляются в Git или Dockerfile.

Портал при старте открывает соединения с PostgreSQL. Если они не установлены, запуск завершится ошибкой пула соединений; исправление import/PORT само по себе не устраняет недоступность базы. `/api/health` проверяет HTTP-процесс после запуска, а не актуальность данных или доступность всех внешних сервисов. Для логина дополнительно нужен доступ к Supabase Auth.

## Проверка

После успешного деплоя:

```sh
curl -i https://xx.chaika.team/api/health
curl -i https://xx.chaika.team/api/me
```

Ожидается 200 и `{"status":"ok"}` у первого запроса, 401 без сессии у второго. Корень `/` в образе без React возвращает сведения об API с кодом 200. Страницы сайта публикуются отдельно на `dashboard.chaika.team`.

Для локальной проверки образа без рабочих данных:

```sh
docker build -t chaika-backend:ci .
python3 tools/smoke_container.py chaika-backend:ci
```

Скрипт создаёт отдельную Docker-сеть и временный PostgreSQL, запускает приложение на 8000 и 9123, проверяет HTTP из другого контейнера и удаляет только созданные им контейнеры и сеть. Эта проверка также включена в GitHub Actions.

Документация платформы: [FastAPI](https://timeweb.cloud/docs/apps/deploying-backend-applications/fastapi), [Dockerfile и EXPOSE](https://timeweb.cloud/docs/apps/deploying-with-dockerfile), [путь проверки состояния](https://timeweb.cloud/docs/apps/healthcheck-path).
