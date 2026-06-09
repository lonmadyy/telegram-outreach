# telegram-outreach — Архитектура и техническое описание

Версия документа: 1.0
Дата: 2026-05-28
Целевая платформа: Linux VPS (production) / Windows (development)
Назначение: автоматизация рассылки сообщений и инвайтов в Telegram-чат по списку username с использованием нескольких пользовательских аккаунтов, с упором на устойчивость к ограничениям Telegram и удобство управления через Telegram-бот.

---

## Оглавление

1. [Назначение и функциональность](#1-назначение-и-функциональность)
2. [Технический стек и обоснование](#2-технический-стек-и-обоснование)
3. [Высокоуровневая архитектура](#3-высокоуровневая-архитектура)
4. [Модель данных PostgreSQL](#4-модель-данных-postgresql)
5. [Антибан-стратегия](#5-антибан-стратегия)
6. [Алгоритмы воркеров](#6-алгоритмы-воркеров)
7. [SpamBot мониторинг и парсинг](#7-spambot-мониторинг-и-парсинг)
8. [Шаблоны сообщений (переменные и spintax)](#8-шаблоны-сообщений-переменные-и-spintax)
9. [Очередь задач и атомарное распределение](#9-очередь-задач-и-атомарное-распределение)
10. [Управляющий Telegram-бот: сценарии и команды](#10-управляющий-telegram-бот-сценарии-и-команды)
11. [Конфигурация и переменные окружения](#11-конфигурация-и-переменные-окружения)
12. [Структура проекта](#12-структура-проекта)
13. [Развёртывание (Docker, VPS)](#13-развёртывание-docker-vps)
14. [Безопасность](#14-безопасность)
15. [Логирование](#15-логирование)
16. [Обработка ошибок Telethon](#16-обработка-ошибок-telethon)
17. [План разработки по этапам](#17-план-разработки-по-этапам)
18. [Эксплуатация и резервное копирование](#18-эксплуатация-и-резервное-копирование)
19. [Расширения за пределами v1](#19-расширения-за-пределами-v1)

---

## 1. Назначение и функциональность

### 1.1. Что делает софт

`telegram-outreach` — это сервис для управляемой автоматизации двух типов действий в Telegram, выполняемых от имени пользовательских (userbot) аккаунтов:

1. **Рассылка личных сообщений** по списку username.
2. **Приглашение (invite) пользователей** в указанный целевой чат (супергруппа или канал, в котором рабочие аккаунты обладают правом приглашать).

Списки клиентов передаются в виде TXT-файла (по одному username в строке, с префиксом `@` или без). За один запуск кампании выполняется **только один** из режимов (рассылка или инвайт) — это упрощает логику и снижает количество подозрительных паттернов активности.

### 1.2. Ключевые свойства

- **Многоаккаунтная работа.** До 10 одновременных рабочих аккаунтов, каждый обрабатывает свою часть списка. Распределение задач **динамическое** через очередь в БД, а не статическое заранее: если один аккаунт ушёл в пауз/флуд, его «нераспределённые» задачи возьмёт другой свободный аккаунт.
- **Глобальный дедуп.** Любой клиент, по которому отправлено сообщение или инвайт, фиксируется в таблице `processed_clients`. Перед каждой попыткой воркер проверяет таблицу — если клиент там есть, он пропускается. Это работает между перезапусками и между разными аккаунтами.
- **Случайные интервалы.** Между действиями каждого аккаунта пауза 300-540 секунд + джиттер ±15% (≈ 255-621 сек фактического разброса).
- **Тихие часы.** С 01:00 до 07:00 по Europe/Minsk все воркеры стоят. Возобновление с 07:00 случайно растягивается на 10-15 минут, чтобы не было одновременного «залпа» всех аккаунтов.
- **Самостоятельная пауза при ограничениях.** При `FloodWaitError`/`PeerFloodError` аккаунт ставит сам себя в состояние `pause` или `spam_blocked`, не валит софт, остальные продолжают работать.
- **Мониторинг через @SpamBot.** Каждые 240 секунд (4 минуты) каждый аккаунт отправляет `/start` в `@SpamBot`, парсит ответ, обновляет свой статус. Если ответ говорит о снятии ограничений — аккаунт автоматически возвращается в работу даже если по таймеру `FloodWait` ещё час впереди.
- **Адаптивные лимиты.** После `PeerFloodError` дневной лимит аккаунта снижается до 75% от стандартного. Повторные `PeerFloodError` не стакают снижение (остаётся 75%). Возврат к 100% — только после подтверждения от SpamBot, что ограничений нет.
- **Warmup новых аккаунтов.** Свежедобавленный аккаунт первые 24-48 часов работает по щадящему сценарию (онлайн, чтение каналов, минимум исходящей активности) и только потом переходит в полный лимит.
- **Управление через Telegram-бот.** Отдельный управляющий бот (созданный пользователем в @BotFather) принимает команды от заданного admin user_id: добавление/удаление аккаунтов, загрузка списка, запуск/пауза/возобновление кампаний, статус, экспорт логов.
- **Структурированное логирование.** Все значимые события пишутся в БД и в файл с ротацией. «Бездейственные» вызовы API (resolve, GetUsers) не логируются.
- **Деплой через Docker Compose** на любом Linux VPS, состояние сохраняется в volume.

### 1.3. Чего софт НЕ делает (вне scope v1)

- Не покупает и не регистрирует новые аккаунты Telegram.
- Не парсит чужие группы для сбора username (нужен готовый список).
- Не отправляет ответы и не ведёт диалоги — это однонаправленный outreach.
- Не выполняет «инвайт через ссылку» (только через `InviteToChannelRequest` от имени аккаунта-инвайтера, который должен быть участником целевого чата с правами).
- Не масштабируется горизонтально на несколько серверов (одна инсталляция = одна машина).

---

## 2. Технический стек и обоснование

| Слой | Технология | Версия | Назначение |
|---|---|---|---|
| Язык | Python | 3.12 | Основной язык, нативный asyncio |
| Telegram MTProto | Telethon | >= 1.36 | Управление userbot-аккаунтами |
| Telegram Bot API | aiogram | >= 3.4 | Управляющий бот для администратора |
| СУБД | PostgreSQL | 16 | Состояние, очередь задач, логи |
| Драйвер БД | asyncpg + SQLAlchemy 2.0 async | latest | ORM + сырой async доступ для критичных запросов |
| Миграции | Alembic | latest | Версионирование схемы |
| Планировщик | APScheduler | >= 3.10 | Периодические задачи (SpamBot, прогресс) |
| Конфигурация | pydantic-settings | >= 2 | Типизированные настройки из env |
| Логирование | loguru + structlog | latest | Файловый лог с ротацией + структурированные события |
| Шифрование | cryptography (Fernet) | latest | Опциональное шифрование чувствительных полей |
| Контейнеризация | Docker + Docker Compose | latest | Деплой |
| Process manager | Docker `restart: unless-stopped` | — | Автоперезапуск |

### 2.1. Почему Telethon, а не Pyrogram

Решение принято на этапе обсуждения архитектуры. Основания:

1. **Темп поддержки.** Telethon — активно развивается, релизы выходят регулярно, оперативно реагируют на изменения в MTProto-слое и API-уровне Telegram. Pyrogram периодически замедлялся в развитии, его форки (kurigram, hydrogram) — попытки сохранить актуальность, но в долгосрочной перспективе это риск.
2. **Типизированные исключения.** Telethon выбрасывает конкретные классы — `FloodWaitError`, `PeerFloodError`, `UserPrivacyRestrictedError`, `UserNotMutualContactError`, `ChatAdminRequiredError`, `ChannelPrivateError`, `UserDeactivatedError` и т.д. Это позволяет точечно обрабатывать каждый сценарий. Pyrogram чаще оборачивает их в общий `RPCError` с парсингом строкового кода.
3. **Низкоуровневый доступ к raw API.** Для тонкого антибан-поведения (имитация типинга, чтения, специфические запросы вроде `messages.SetTyping`) нужен прямой доступ к MTProto-методам. Telethon реализует это через `client(<RawRequest>)` без обёрток-абстракций.
4. **Хранилище сессий.** Telethon из коробки поддерживает `SQLiteSession`, `StringSession`, `MemorySession`. Сессии портабельны между платформами (мы используем файловые `.session` на volume).
5. **Зрелое сообщество.** Большее количество готовых паттернов, обсуждений edge-кейсов, проверенных конфигураций для anti-flood.

### 2.2. Почему PostgreSQL, а не SQLite

При параллельной работе 3-10 воркеров мы получаем конкурентные записи в `processed_clients` и `tasks`. SQLite даже в WAL-режиме периодически выдаёт `database is locked` под такой нагрузкой, и не имеет нормального механизма блокировки строк для атомарной выдачи задачи (`SELECT ... FOR UPDATE SKIP LOCKED`). PostgreSQL решает обе проблемы нативно. В Docker Compose добавление сервиса БД — одна строка, оверхеда нет.

### 2.3. Почему aiogram, а не python-telegram-bot

aiogram 3.x — современный async-первый фреймворк с продуманной системой роутеров, FSM из коробки, типизация всех апдейтов. python-telegram-bot традиционен и стабилен, но его FSM (для сценариев добавления аккаунта с 2FA, загрузки файла, выбора шаблона) требует больше boilerplate.

### 2.4. Почему интерфейс — Telegram-бот, а не веб

Веб-интерфейс был запасным вариантом. Telegram-бот выбран потому, что:
- Не нужен открытый порт на VPS наружу (меньше attack surface).
- Управление с телефона без отдельных приложений.
- Естественная интеграция с экосистемой (бот может отправить любой файл, получить документ через `/upload`).

Опциональный read-only веб-дашборд для просмотра больших объёмов логов оставлен как расширение для v1.1 (см. раздел 19).

---

## 3. Высокоуровневая архитектура

### 3.1. Диаграмма компонентов

```
                    ┌──────────────────────────────────────────────┐
                    │              VPS (Linux)                      │
                    │                                                │
                    │  ┌──────────────┐    ┌────────────────────┐  │
                    │  │  Postgres    │    │   App container     │  │
                    │  │  container   │◄───┤                     │  │
                    │  │  :5432       │    │  ┌───────────────┐  │  │
                    │  └──────────────┘    │  │  Bot UI       │  │  │
                    │         ▲             │  │  (aiogram)    │  │  │
                    │         │             │  └──────┬────────┘  │  │
                    │         │             │         │            │  │
                    │  ┌──────┴────────┐   │  ┌──────▼────────┐  │  │
                    │  │  Volume:      │   │  │  Campaign     │  │  │
                    │  │  data/        │   │  │  Manager      │  │  │
                    │  │   ├─sessions/ │◄──┤  └──────┬────────┘  │  │
                    │  │   └─logs/     │   │         │            │  │
                    │  └───────────────┘   │  ┌──────▼────────┐  │  │
                    │                       │  │  Worker Pool  │  │  │
                    │                       │  │  (Telethon ×N)│──┼──┼──► Telegram
                    │                       │  └──────┬────────┘  │  │   MTProto
                    │                       │         │            │  │
                    │                       │  ┌──────▼────────┐  │  │
                    │                       │  │  Scheduler    │  │  │
                    │                       │  │  (APScheduler)│  │  │
                    │                       │  └───────────────┘  │  │
                    │                       └─────────────────────┘  │
                    └──────────────────────────────────────────────┘
                                            │
                                            ▼
                                     ┌──────────────┐
                                     │  Admin user  │
                                     │  (в Telegram)│
                                     └──────────────┘
```

### 3.2. Компоненты

#### Bot UI
Управляющий бот на aiogram. Отвечает на команды и кнопки **только** от пользователей из whitelist (`ALLOWED_USER_IDS` в `.env`). Реализует FSM-сценарии:
- Добавление userbot-аккаунта (телефон → код → 2FA пароль → прокси).
- Загрузка TXT-списка клиентов.
- Создание/выбор шаблона сообщения.
- Запуск/пауза/остановка кампании.
- Просмотр статуса, выгрузка логов, изменение лимитов.

#### Campaign Manager
Оркестратор уровня кампании. Поднимается при старте приложения, постоянно подписан на изменения через `LISTEN/NOTIFY` Postgres. Отвечает за:
- Парсинг загруженного TXT, дедупликацию относительно `processed_clients`.
- Создание задач в таблице `tasks`.
- Управление состоянием кампании (`pending`, `running`, `paused`, `done`, `failed`).
- Агрегация прогресса для уведомлений.

#### Worker Pool
Пул асинхронных задач, по одной на активный аккаунт. Каждая задача:
- Открывает Telethon-клиент с собственным `.session` и прокси (если задан).
- В цикле забирает следующую задачу через `SELECT ... FOR UPDATE SKIP LOCKED`.
- Применяет антибан-обёртку (см. раздел 5).
- Выполняет действие, фиксирует результат.
- Спит случайные 300-540 секунд (с учётом quiet hours).
- При ошибках обновляет свой `status` и `spam_unlock_at`.

#### Scheduler
APScheduler с asyncio job store. Периодические задачи:
- `spamcheck_job` — раз в `SPAMCHECK_INTERVAL_SEC` (240) для каждого аккаунта: `/start` в `@SpamBot`, парсинг.
- `progress_notify_job` — раз в `PROGRESS_NOTIFY_INTERVAL_SEC` (1800) — сводка по активным кампаниям в управляющий бот.
- `daily_limit_reset_job` — каждый день в 00:00 UTC сбрасывает `daily_sent_count` и `daily_invited_count`.
- `quiet_hours_check_job` — раз в минуту проверяет, в quiet hours ли сейчас, и ставит/снимает глобальный флаг.

#### Postgres
Хранит всё критическое состояние:
- Аккаунты, прокси, статусы, таймеры.
- Кампании, шаблоны, задачи.
- Глобальный реестр обработанных клиентов.
- Лог-события.
- Настройки, изменяемые в рантайме.

#### Volume `data/`
- `data/sessions/` — `.session` файлы Telethon (chmod 600).
- `data/logs/` — ротируемые лог-файлы.
- Включён в стратегию резервного копирования.

---

## 4. Модель данных PostgreSQL

Используется SQLAlchemy 2.0 в декларативном стиле + Alembic для миграций. Ниже описание таблиц в SQL-нотации.

### 4.1. Таблица `accounts`

Хранит userbot-аккаунты. Один аккаунт = один Telethon-клиент = одна сессия.

```sql
CREATE TYPE account_status AS ENUM (
    'warmup',         -- новый, в режиме прогрева
    'active',         -- работает в полном режиме
    'pause',          -- временно недоступен (FloodWait или quiet hours)
    'spam_blocked',   -- получил PeerFlood или SpamBot подтвердил ограничение
    'dead',           -- сессия невалидна, нужна реавторизация
    'disabled'        -- админ отключил вручную
);

CREATE TABLE accounts (
    id              SERIAL PRIMARY KEY,
    phone           VARCHAR(20) UNIQUE NOT NULL,
    tg_user_id      BIGINT UNIQUE,           -- id в Telegram, заполняется после первой авторизации
    username        VARCHAR(64),              -- @username, если есть
    first_name      VARCHAR(128),
    session_path    VARCHAR(255) NOT NULL,    -- 'data/sessions/79991234567.session'
    proxy_url       VARCHAR(255),             -- 'socks5://user:pass@host:port' или NULL
    status          account_status NOT NULL DEFAULT 'warmup',
    spam_unlock_at  TIMESTAMPTZ,              -- когда снимется текущая пауза
    warmup_until    TIMESTAMPTZ,              -- до какого момента действует warmup
    daily_sent      INT NOT NULL DEFAULT 0,
    daily_invited   INT NOT NULL DEFAULT 0,
    last_reset_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    limit_reduced_until TIMESTAMPTZ,           -- адаптивное снижение лимита (75%) действует до этой даты
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at    TIMESTAMPTZ,
    
    CONSTRAINT phone_format CHECK (phone ~ '^\+?[0-9]{7,15}$')
);

CREATE INDEX idx_accounts_status ON accounts(status) WHERE status IN ('active', 'pause');
CREATE INDEX idx_accounts_unlock ON accounts(spam_unlock_at) WHERE spam_unlock_at IS NOT NULL;
```

**Пояснения:**
- `spam_unlock_at` — единое поле для всех типов пауз (FloodWait, PeerFlood-карантин, ручная пауза). NULL = пауз нет.
- `pause_reason` — метка причины паузы (`'flood_wait'` §6.3 / `'quiet_hours'` §5.3) для различения и видимости в `/status` и `/floodwait`. Сбрасывается в NULL при возврате в `active`/`spam_blocked`/`disabled`. На логику снятия паузы не влияет (её делает `is_pause_expired` по `spam_unlock_at`).
- `limit_reduced_until` отдельно: даже когда `spam_unlock_at` истёк и аккаунт снова `active`, лимит может оставаться сниженным до получения положительного ответа от SpamBot.
- `warmup_until` фиксирует, до какого момента применяются warmup-лимиты.

### 4.2. Таблица `proxies` (опциональная, для удобства)

Если у пользователя пул прокси, можно их хранить отдельно и привязывать к аккаунтам. В v1 — необязательно, можно хранить прямо в `accounts.proxy_url`.

### 4.3. Таблица `templates`

```sql
CREATE TABLE templates (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(128) UNIQUE NOT NULL,
    body            TEXT NOT NULL,
    -- Список переменных, требуемых шаблоном, для валидации:
    variables       JSONB NOT NULL DEFAULT '[]'::JSONB,  -- ['username', 'first_name']
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

`body` хранится с поддержкой spintax (`{a|b|c}`) и переменных (`{username}`).

### 4.4. Таблица `campaigns`

```sql
CREATE TYPE campaign_type AS ENUM ('message', 'invite');
CREATE TYPE campaign_status AS ENUM (
    'pending',    -- создана, ожидает старта
    'running',    -- идёт
    'paused',     -- приостановлена админом или системой (все аки в флуде)
    'done',       -- завершена нормально
    'failed',     -- завершена с критической ошибкой
    'cancelled'   -- отменена админом
);

CREATE TABLE campaigns (
    id                  SERIAL PRIMARY KEY,
    type                campaign_type NOT NULL,
    template_id         INT REFERENCES templates(id),       -- NULL для invite
    target_chat         VARCHAR(255),                        -- '@channel' или '-1001234567890' для invite
    target_chat_id      BIGINT,                              -- разрешённый id, кэшируется при старте
    status              campaign_status NOT NULL DEFAULT 'pending',
    total_count         INT NOT NULL DEFAULT 0,
    sent_count          INT NOT NULL DEFAULT 0,
    skipped_count       INT NOT NULL DEFAULT 0,
    failed_count        INT NOT NULL DEFAULT 0,
    resend_old          BOOLEAN NOT NULL DEFAULT FALSE,      -- переотправить тем, кому писали >6мес назад
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    created_by_user_id  BIGINT NOT NULL,                     -- admin user_id в Telegram
    notes               TEXT
);

CREATE INDEX idx_campaigns_status ON campaigns(status) WHERE status IN ('running', 'paused');
```

### 4.5. Таблица `tasks`

Очередь задач. Один ряд = один username, который нужно обработать в рамках кампании.

```sql
CREATE TYPE task_status AS ENUM (
    'queued',
    'in_progress',
    'done',
    'failed',
    'skipped'
);

CREATE TYPE result_code AS ENUM (
    'ok',                    -- успешно отправлено/приглашено
    'flood_wait',            -- получили FloodWait, будем ретраить
    'peer_flood',            -- PeerFlood — серьёзно, ретрай не сразу
    'privacy_restricted',    -- юзер запретил приватные сообщения от не-контактов
    'not_mutual_contact',    -- для invite: нужно быть взаимным контактом
    'not_found',             -- username не существует или удалён
    'already_member',        -- для invite: уже в чате
    'channel_private',
    'too_many_channels',     -- юзер уже в максимуме каналов
    'banned_in_channel',
    'deactivated',           -- аккаунт получателя удалён
    'other_error'
);

CREATE TABLE tasks (
    id                  BIGSERIAL PRIMARY KEY,
    campaign_id         INT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    username            VARCHAR(64) NOT NULL,
    status              task_status NOT NULL DEFAULT 'queued',
    assigned_account_id INT REFERENCES accounts(id),
    attempts            INT NOT NULL DEFAULT 0,
    last_attempt_at     TIMESTAMPTZ,
    result_code         result_code,
    error_message       TEXT,
    locked_until        TIMESTAMPTZ,           -- для retry с задержкой
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at        TIMESTAMPTZ
);

-- Индекс для атомарной выдачи задач воркеру (SKIP LOCKED):
CREATE INDEX idx_tasks_queue ON tasks(campaign_id, status, locked_until)
    WHERE status = 'queued';

CREATE UNIQUE INDEX idx_tasks_campaign_username ON tasks(campaign_id, username);
```

**Ключевые моменты:**
- Уникальный индекс `(campaign_id, username)` — внутри одной кампании дубликаты невозможны.
- `locked_until` используется когда нужен отложенный retry (например, после `FloodWait` на 60 сек задача отдыхает в очереди).
- `assigned_account_id` фиксируется в момент захвата задачи, но если этот аккаунт впоследствии не справился (ушёл в флуд), задача возвращается в очередь и может быть взята другим аккаунтом.

### 4.6. Таблица `processed_clients`

**Глобальный** реестр обработанных клиентов между всеми кампаниями.

```sql
CREATE TABLE processed_clients (
    username            VARCHAR(64) PRIMARY KEY,
    last_action         VARCHAR(16) NOT NULL,   -- 'message' | 'invite'
    last_result_code    result_code NOT NULL,
    first_processed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_processed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    account_id          INT REFERENCES accounts(id),
    campaign_id         INT REFERENCES campaigns(id)
);

CREATE INDEX idx_processed_last_at ON processed_clients(last_processed_at);
```

**Логика проверки при подготовке кампании:**
```python
if campaign.resend_old:
    # Пропускаем только тех, кого обрабатывали недавно
    cutoff = now - timedelta(days=180)
    skip_usernames = SELECT username FROM processed_clients WHERE last_processed_at >= cutoff
else:
    # Пропускаем всех, кто был обработан хоть когда-то
    skip_usernames = SELECT username FROM processed_clients
```

Запись в `processed_clients` происходит **только при `result_code IN ('ok')`** — то есть фактически после успешного действия. Ошибки (`not_found`, `privacy_restricted` и т.д.) попадают в отчёт кампании, но не блокируют переотправку в будущем (вдруг юзер сменил настройки приватности).

### 4.7. Таблица `spam_check_history`

```sql
CREATE TABLE spam_check_history (
    id              BIGSERIAL PRIMARY KEY,
    account_id      INT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    raw_response    TEXT NOT NULL,           -- полный текст ответа SpamBot
    parsed_status   VARCHAR(32) NOT NULL,    -- 'no_limits' | 'soft_warning' | 'temporary' | 'permanent' | 'unknown'
    unlock_at       TIMESTAMPTZ              -- если есть конкретная дата снятия
);

CREATE INDEX idx_spam_check_account_time ON spam_check_history(account_id, checked_at DESC);
```

В обычном режиме записываем **только изменения статуса** (если предыдущая проверка дала тот же результат — не пишем), чтобы не разрастаться. Сырой ответ нужен для разбора нестандартных формулировок (например, на разных языках).

### 4.8. Таблица `logs`

```sql
CREATE TYPE log_level AS ENUM ('debug', 'info', 'warning', 'error', 'critical');

CREATE TABLE logs (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    level           log_level NOT NULL,
    event_type      VARCHAR(64) NOT NULL,    -- 'message_sent', 'flood_wait', и т.д.
    account_id      INT REFERENCES accounts(id) ON DELETE SET NULL,
    campaign_id     INT REFERENCES campaigns(id) ON DELETE SET NULL,
    task_id         BIGINT REFERENCES tasks(id) ON DELETE SET NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::JSONB,
    message         TEXT NOT NULL
);

CREATE INDEX idx_logs_ts ON logs(ts DESC);
CREATE INDEX idx_logs_account_ts ON logs(account_id, ts DESC);
CREATE INDEX idx_logs_campaign_ts ON logs(campaign_id, ts DESC);
CREATE INDEX idx_logs_event ON logs(event_type);
```

Параллельно ведётся файловый лог (с ротацией) — на случай если БД недоступна, мы не потеряем диагностику.

### 4.9. Таблица `settings`

Настройки, изменяемые без рестарта:

```sql
CREATE TABLE settings (
    key             VARCHAR(64) PRIMARY KEY,
    value           TEXT NOT NULL,
    value_type      VARCHAR(16) NOT NULL,   -- 'int', 'float', 'bool', 'str', 'json'
    description     TEXT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by      BIGINT                   -- admin user_id
);
```

При изменении настройки через бот выполняется `pg_notify('settings_changed', key)`. Приложение слушает этот канал через `asyncpg.Connection.add_listener`, обновляет кэш в памяти. Так лимиты, интервалы, quiet hours меняются «на лету».

Дефолтные настройки заполняются при первой миграции:
```
daily_dm_limit_warm = 40
daily_invite_limit_warm = 100
daily_dm_limit_fresh = 10
daily_invite_limit_fresh = 5
interval_min_sec = 300
interval_max_sec = 540
spamcheck_interval_sec = 240
progress_notify_interval_sec = 1800
quiet_hours_start = "01:00"
quiet_hours_end = "07:00"
quiet_hours_timezone = "Europe/Minsk"
peerflood_limit_ratio = 0.75
warmup_duration_hours = 48
adaptive_limit_reduction_days = 7
```

---

## 5. Антибан-стратегия

Это самая важная часть архитектуры. Telegram имеет несколько слоёв защиты от спама, и наша задача — минимизировать вероятность попасть под каждый из них.

### 5.1. Уровень аккаунта

#### Warmup для новых аккаунтов
Свежий аккаунт (добавленный менее 48 часов назад) выглядит подозрительно, если сразу начинает массовую активность. Сценарий warmup:

| Период с момента добавления | Действие | Дневной лимит DM | Дневной лимит invite |
|---|---|---|---|
| 0-2 часа | Только онлайн, чтение «своих» сообщений | 0 | 0 |
| 2-12 часов | Подписка на 3-5 публичных каналов (списки в коде), периодическое чтение | 0 | 0 |
| 12-24 часа | Возможна отправка в Saved Messages для имитации активности | 3 | 0 |
| 24-48 часов | Половинный лимит | 10 | 5 |
| > 48 часов | Полный лимит | 40 | 100 |

Поле `accounts.warmup_until` хранит timestamp окончания warmup. Логика лимита смотрит сначала на это поле, потом на `limit_reduced_until` (адаптивное снижение).

Каналы для warmup-подписки задаются в `config.py` константой (например, крупные новостные/тематические каналы — Pavel Durov, Telegram Tips и т.д.). Это безопасно, не палится, делает «историю» аккаунта более правдоподобной.

Реализация (MVP-5, гибрид): подписка на `WARMUP_CHANNELS` (constants.py) выполняется разово при добавлении аккаунта, пока клиент авторизован; далее воркер во время warmup-периода периодически «оживляет» аккаунт (онлайн-статус + чтение случайного канала, опционально Saved Messages в 12–24ч). По истечении `warmup_until` воркер сам переводит аккаунт `warmup → active`. DB-снимок участников/расширенный сценарий — за пределами v1.

#### Дневные лимиты
- DM (рассылка): **40** для прогретых, **10** для warmup.
- Invite: **100** для прогретых, **5** для warmup.
- При `PeerFloodError` → лимит снижается до **75%** от стандартного (30 DM / 75 invite), `limit_reduced_until = now + 7 дней`. Повторные PeerFlood в течение этих 7 дней лимит не снижают дальше, остаётся 75%.
- Возврат к 100% только после положительного ответа SpamBot («no limits»).
- Счётчики `daily_sent` и `daily_invited` сбрасываются каждый день в 00:00 UTC (cron в APScheduler).

#### Состояния и переходы
```
            ┌──────────┐
            │  warmup  │  (создание аккаунта)
            └────┬─────┘
                 │ +48h
                 ▼
            ┌──────────┐  FloodWait/QuietHours/Daily limit reached
   ┌────────│  active  │────────────┐
   │        └────┬─────┘            │
   │             │ PeerFlood        ▼
   │             ▼              ┌─────────┐
   │       ┌──────────────┐     │  pause  │ (FloodWait_until, спит)
   │       │ spam_blocked │     └────┬────┘
   │       │ (12+ часов   │          │ время прошло / SpamBot ok
   │       │  ожидания)   │          ▼
   │       └──────┬───────┘     ┌──────────┐
   │              │ SpamBot ok  │  active  │
   │              └────────────►└──────────┘
   │
   │ sessions error
   ▼
┌────────┐
│  dead  │  (нужна реавторизация админом)
└────────┘
```

Переходы инициируются:
- Самим воркером (после получения exception от Telethon).
- Scheduler-задачей `spamcheck_job` (на основе ответа SpamBot).
- Админом через бот-команды.

### 5.2. Уровень действия (имитация человеческого поведения)

#### Перед отправкой DM
```python
# 1. Получаем entity получателя (с кэшированием access_hash)
peer = await get_cached_or_resolve(username)
if peer is None:
    return Result.not_found

# 2. Лёгкая случайная задержка (имитация открытия чата)
await asyncio.sleep(random.uniform(1.5, 4.0))

# 3. Если диалог уже существовал — отправляем read receipt
#    (если не существовал — пропускаем, чтобы не вызвать GetHistoryRequest впустую)
if dialog_exists:
    try:
        await client.send_read_acknowledge(peer)
    except Exception:
        pass

# 4. Имитация набора
async with client.action(peer, 'typing'):
    typing_duration = random.uniform(2.5, 7.0)
    await asyncio.sleep(typing_duration)

# 5. Рендер шаблона (spintax + переменные)
text = template.render(target=peer)

# 6. Собственно отправка
await client.send_message(peer, text)

# 7. Случайная пауза перед следующим циклом
delay = random.uniform(300, 540) * random.uniform(0.85, 1.15)
await asyncio.sleep(delay)
```

#### Перед инвайтом
```python
# 1. Резолв таргета (один раз кэшируется)
peer = await get_cached_or_resolve(username)
if peer is None:
    return Result.not_found

# 2. Проверка членства в целевом чате
if await is_already_member(target_chat_id, peer.id):
    return Result.already_member

# 3. Случайная задержка
await asyncio.sleep(random.uniform(1.5, 4.0))

# 4. Инвайт
try:
    await client(InviteToChannelRequest(target_chat_id, [peer]))
except (UserPrivacyRestrictedError, UserNotMutualContactError) as e:
    return map_invite_error(e)
```

Проверка членства реализуется через кэш `participants_cache`, который заполняется при первом обращении к чату (`iter_participants` с пагинацией и лимитом). **В v1 (MVP-4) кэш хранится только в памяти процесса**; после рестарта он перезаполняется при первом инвайте в чат. Снимок в Postgres для restart — отложен (см. раздел 19). После успешного инвайта `user_id` добавляется в кэш, чтобы параллельные воркеры не пытались пригласить его повторно.

Помимо исключений, успех инвайта подтверждается инспекцией поля `missing_invitees` в ответе `InviteToChannelRequest`: если Telegram молча не добавил пользователя (приватность/премиум-ограничение), это трактуется как `skip` (`privacy_restricted`) — запись в `processed_clients` и инкремент счётчика происходят **только** при фактическом добавлении (§4.6). Имитация набора (`typing`) для инвайта не выполняется — только короткая пауза `pre_action_pause` (в отличие от DM).

### 5.3. Уровень кампании

#### Старт со смещением
При запуске кампании каждый воркер получает `initial_delay = random.uniform(0, 300)` секунд. Это гарантирует, что 5 аккаунтов не начнут отправку синхронно одной минутой.

#### Quiet hours
Между 01:00 и 07:00 (Europe/Minsk) активность приостанавливается. Реализация:
- Scheduler-задача `quiet_hours_check_job` раз в минуту проверяет, в окне ли мы.
- При входе в окно: `accounts.status = 'pause'`, `spam_unlock_at = next_07_00`.
- При выходе из окна: каждый аккаунт получает случайный `spam_unlock_at = 07:00 + uniform(0, 15min)`. Это «размазывает» возобновление.

#### Глобальная пауза при массовом флуде
Если за 30 минут все активные аккаунты получили `PeerFloodError`:
- Кампания переходит в `status = paused`.
- Админ получает уведомление в бот.
- Каждый аккаунт продолжает свой `spamcheck_job`, и при первом «no limits» от SpamBot — кампания автоматически возобновляется.

#### Адаптивный пересчёт интервалов
Если за последний час был хотя бы один PeerFlood в системе, **временно** интервал между действиями повышается до диапазона 450-720 сек (на 1 час). Это эвристика, защищающая от каскадных банов.

### 5.4. Кэширование `access_hash`

Telegram MTProto оперирует не username'ами, а парами `(user_id, access_hash)`. Каждый `client.get_entity('@username')` вызывает RPC, и слишком частое такое — палится. Решение:

- Таблица `peer_cache (username, user_id, access_hash, resolved_at)`.
- При первой обработке username — `get_entity`, сохраняем в кэш.
- При следующих обработках в той же кампании — используем кэш напрямую через `InputPeerUser(user_id, access_hash)`.
- Кэш живёт 7 дней, потом считается устаревшим (юзер мог сменить username).

### 5.5. Что мы НЕ делаем

- Не используем `client.get_dialogs()` для массового перебора — он тяжёлый и палится.
- Не делаем `GetFullUser` для каждого получателя — для отправки сообщения достаточно `InputPeerUser`.
- Не отправляем медиа (картинки/видео) в v1 — медиа-сообщения чаще ловят антиспам.
- Не пытаемся обходить блок через мгновенное переподключение — это бессмысленно и палится.

---

## 6. Алгоритмы воркеров

### 6.1. Жизненный цикл воркера

```python
async def worker_loop(account_id: int):
    account = await load_account(account_id)
    client = await create_telethon_client(account)
    
    try:
        await client.connect()
        
        # Стартовый offset, чтобы аккаунты не били синхронно
        await asyncio.sleep(random.uniform(0, 300))
        
        while not shutdown_requested:
            # 1. Проверка статуса
            account = await refresh_account(account_id)
            
            if account.status == 'disabled':
                break
            
            if account.status == 'dead':
                await notify_admin(f"Аккаунт {account.phone} требует реавторизации")
                break
            
            # 2. Проверка quiet hours
            if is_in_quiet_hours():
                next_check = next_quiet_end_time() + random.uniform(0, 900)
                await sleep_until(next_check)
                continue
            
            # 3. Проверка spam_unlock_at
            if account.spam_unlock_at and account.spam_unlock_at > now():
                # Спим до разблокировки, но просыпаемся каждые 30 сек
                # на случай если SpamBot снимет ограничение раньше
                await asyncio.sleep(30)
                continue
            
            # 4. Проверка дневного лимита
            if not can_send_today(account):
                await sleep_until_midnight_utc()
                continue
            
            # 5. Поиск активной кампании и захват задачи
            task = await fetch_next_task(account_id)
            if task is None:
                # Нет работы — короткая пауза и снова проверка
                await asyncio.sleep(30)
                continue
            
            # 6. Выполнение задачи
            try:
                result = await execute_task(client, account, task)
                await mark_task_done(task.id, result)
                
                if result.code == 'ok':
                    await register_processed(task.username, account_id, task.campaign_id)
                    await increment_account_counter(account_id, task.campaign.type)
            
            except FloodWaitError as e:
                await handle_flood_wait(account_id, task.id, e.seconds)
                continue
            
            except PeerFloodError:
                await handle_peer_flood(account_id, task.id)
                continue
            
            except Exception as e:
                await log_unexpected_error(account_id, task.id, e)
                await mark_task_failed(task.id, 'other_error', str(e))
            
            # 7. Случайная пауза с джиттером
            delay = random.uniform(
                settings.interval_min_sec,
                settings.interval_max_sec
            )
            delay *= random.uniform(0.85, 1.15)
            await asyncio.sleep(delay)
    
    finally:
        await client.disconnect()
```

### 6.2. Захват задачи (SKIP LOCKED)

```sql
WITH next_task AS (
    SELECT id FROM tasks
    WHERE campaign_id IN (SELECT id FROM campaigns WHERE status = 'running')
      AND status = 'queued'
      AND (locked_until IS NULL OR locked_until <= NOW())
    ORDER BY id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE tasks
SET status = 'in_progress',
    assigned_account_id = :account_id,
    attempts = attempts + 1,
    last_attempt_at = NOW()
WHERE id = (SELECT id FROM next_task)
RETURNING *;
```

`SKIP LOCKED` гарантирует, что параллельные воркеры не возьмут одну и ту же задачу. Если задача никем не залочена — её захватит первый воркер, остальные пройдут мимо.

Захват дополнительно фильтруется (MVP-4/5): `exclude_campaign_ids` исключает invite-кампании, в чат которых аккаунт не может приглашать (§5.3); `allowed_types` ограничивает типами, доступными по дневному лимиту (§5.1) — warmup-аккаунт с `invite=0` физически не захватит invite-задачу. Это и есть «реальная проверка по типу» — без отката после захвата и без busy-loop.

### 6.3. Обработка FloodWait

```python
async def handle_flood_wait(account_id, task_id, seconds):
    # Не возмем эту задачу повторно слишком быстро
    await db.execute(
        "UPDATE tasks SET status = 'queued', locked_until = NOW() + INTERVAL '%s seconds' WHERE id = %s",
        seconds, task_id
    )
    
    unlock_at = now() + timedelta(seconds=seconds)
    await db.execute(
        "UPDATE accounts SET status = 'pause', spam_unlock_at = %s WHERE id = %s",
        unlock_at, account_id
    )
    
    await log_event(
        level='warning',
        event_type='flood_wait',
        account_id=account_id,
        task_id=task_id,
        payload={'seconds': seconds},
        message=f"FloodWait {seconds}s, аккаунт на паузе до {unlock_at}"
    )
```

При паузе также пишется `pause_reason='flood_wait'` (`accounts_repo.set_pause(..., reason="flood_wait")`) — чтобы отличать FloodWait-паузу от ночной quiet-паузы (та же `status='pause'`, §5.3). Видимость: хелпер `is_flood_waiting()`, метка `⏳ FloodWait` в `/status` и команда `/floodwait` (§10.2). Активного опроса остатка FloodWait нет (Telegram его не отдаёт, а лишний запрос продлевает блок) — отслеживается реактивно по уже пойманным событиям `flood_wait` в таблице `logs`.

### 6.4. Обработка PeerFlood

```python
async def handle_peer_flood(account_id, task_id):
    # PeerFlood — более серьёзно, ставим карантин на 12 часов
    unlock_at = now() + timedelta(hours=12)
    limit_reduced_until = now() + timedelta(days=7)
    
    await db.execute("""
        UPDATE accounts
        SET status = 'spam_blocked',
            spam_unlock_at = %s,
            limit_reduced_until = COALESCE(limit_reduced_until, %s)
        WHERE id = %s
    """, unlock_at, limit_reduced_until, account_id)
    
    await db.execute(
        "UPDATE tasks SET status = 'queued', locked_until = %s WHERE id = %s",
        unlock_at, task_id
    )
    
    # Принудительный SpamBot-чек прямо сейчас
    await trigger_spam_check(account_id)
    
    await log_event(
        level='error',
        event_type='peer_flood',
        account_id=account_id,
        task_id=task_id,
        message="PeerFlood: 12ч карантин, лимит снижен до 75% на 7 дней"
    )
    
    await notify_admin(f"Аккаунт {account.phone} получил PeerFlood")
```

Важно: `COALESCE(limit_reduced_until, ...)` — если адаптивное снижение уже действует, мы не «продлеваем» его повторными PeerFlood. Возврат к 100% — только после ok от SpamBot.

### 6.5. Возврат к 100% лимиту

В `spamcheck_job` после парсинга ответа SpamBot:
```python
if parsed.status == 'no_limits' and account.limit_reduced_until:
    await db.execute(
        "UPDATE accounts SET limit_reduced_until = NULL, status = 'active', spam_unlock_at = NULL WHERE id = %s",
        account_id
    )
    await log_event(
        level='info',
        event_type='limit_restored',
        account_id=account_id,
        message="SpamBot подтвердил отсутствие ограничений, лимит восстановлен"
    )
```

---

## 7. SpamBot мониторинг и парсинг

### 7.1. Расписание
APScheduler-задача `spamcheck_job` запускается **каждые 240 секунд** для **каждого активного или paused/spam_blocked аккаунта**. Для `disabled` и `dead` — пропускается.

### 7.2. Алгоритм

```python
async def spam_check(account_id: int):
    client = get_worker_client(account_id)
    if client is None or not client.is_connected():
        return
    
    try:
        # Отправляем /start
        await client.send_message('SpamBot', '/start')
        
        # Ждём ответа (с таймаутом)
        async with client.conversation('SpamBot', timeout=30) as conv:
            response = await conv.get_response()
        
        parsed = parse_spambot_response(response.text)
        
        # Сравниваем с предыдущим статусом
        prev_status = await get_last_parsed_status(account_id)
        if parsed.status != prev_status:
            await save_spam_check(account_id, response.text, parsed)
            await react_to_status_change(account_id, parsed)
    
    except Exception as e:
        # Логируем только критические ошибки, чтобы не флудить
        if not isinstance(e, asyncio.TimeoutError):
            await log_event('warning', 'spamcheck_failed', account_id=account_id, message=str(e))
```

### 7.3. Парсинг ответов

SpamBot отвечает шаблонно, но с вариациями по языку и форме. Подход — pattern matching через regex с приоритетом конкретных шаблонов.

```python
def parse_spambot_response(text: str) -> ParsedSpamStatus:
    lower = text.lower()
    
    # 1. Жёсткий блок с датой
    m = re.search(
        r'(?:until|до)\s+(\d{1,2}\s+\w+\s+\d{4}.*?\d{1,2}:\d{2}.*?utc)',
        text, re.IGNORECASE
    )
    if m:
        unlock_at = parse_datetime(m.group(1))
        return ParsedSpamStatus('temporary', unlock_at=unlock_at)
    
    # 2. Бессрочный блок
    if any(k in lower for k in ['permanently', 'unable to lift', 'навсегда']):
        return ParsedSpamStatus('permanent')
    
    # 3. Чисто
    if any(k in lower for k in [
        'good news', 'no limits', 'no restrictions',
        'хорошие новости', 'нет ограничений',
        'ограничения сняты', 'ограничений нет'
    ]):
        return ParsedSpamStatus('no_limits')
    # Реальный русский ответ «Ваш аккаунт свободен от каких-либо ограничений»:
    # сочетание «свобод» + «ограничен». Блоки 1–2 уже отсеяли temporary/permanent,
    # поэтому блокировки (где есть «ограничен», но нет «свобод») сюда не попадут.
    if 'свобод' in lower and 'ограничен' in lower:
        return ParsedSpamStatus('no_limits')
    
    # 4. Soft warning ("some users may consider your messages as spam")
    if 'spam' in lower and 'may' in lower:
        return ParsedSpamStatus('soft_warning')
    
    return ParsedSpamStatus('unknown')
```

В `unknown`-случае сырой ответ всегда сохраняется в `spam_check_history.raw_response` для ручного анализа. Уведомление админу не отправляется (чтобы не спамить), но при следующем `/status` админ увидит флаг «неизвестный статус, проверьте лог».

### 7.4. Реакция на изменение статуса

| Был | Стал | Действие |
|---|---|---|
| `no_limits` или `soft_warning` | `temporary` | `status = spam_blocked`, `spam_unlock_at = parsed.unlock_at` |
| любой | `permanent` | `status = dead`, уведомление админу: «Аккаунт перманентно ограничен» |
| `temporary` или `spam_blocked` | `no_limits` | `status = active`, `spam_unlock_at = NULL`, `limit_reduced_until = NULL` |
| любой | `soft_warning` | продолжаем работу, флагуется в логах, не меняет статус |
| любой | `unknown` | логируется, статус не меняется |

### 7.5. Контроль частоты обращений к SpamBot

Хотя 4 минуты — это частый интервал, сам факт обращения к боту не считается «спамом» (это публичный системный бот). Но во избежание собственного FloodWait на запросах:
- Между разными аккаунтами обращения к SpamBot **разносятся** во времени: scheduler планирует их с offset (account.id % 240 секунд).
- Если получили FloodWait от обращения к SpamBot — увеличиваем интервал для этого аккаунта вдвое до следующей успешной попытки.

---

## 8. Шаблоны сообщений (переменные и spintax)

### 8.1. Синтаксис

```
Привет, {first_name}! {Как дела|Что нового|Как сам}?

{Хотел|Решил|Думаю} рассказать о новой акции — {у нас|у нашей команды} {скидка|бонус} {до|до} {30|25|35}% на {услуги|сервис}.

Если {интересно|любопытно|зацепило}, {напиши|ответь|пиши}.
```

- `{first_name}`, `{username}` — переменные, подставляются из данных получателя.
- `{a|b|c}` — spintax, случайный выбор одной из альтернатив.
- Вложенность spintax поддерживается (например, `{что-то {новое|старое}|ничего}`).
- Экранирование: `\{` и `\}` если нужны буквальные скобки.

### 8.2. Доступные переменные

| Переменная | Источник | Fallback если нет |
|---|---|---|
| `{username}` | `user.username` | `there` (англ.) или просто пропуск |
| `{first_name}` | `user.first_name` | `there` |
| `{last_name}` | `user.last_name` | пустая строка |
| `{full_name}` | `first_name + last_name` | `there` |

Перед отправкой сообщения происходит `get_entity` (или используется кэш), берутся атрибуты, рендерится текст. Если переменная отсутствует и нет fallback — лог-варнинг, в текст подставляется заранее заданное «нейтральное» слово.

### 8.3. Рендерер

```python
import random
import re

VAR_RE = re.compile(r'(?<!\\)\{([a-z_]+)\}')      # {username}
SPIN_RE = re.compile(r'(?<!\\)\{([^{}]+?)\}')      # {a|b|c} — самые внутренние сначала

def render_template(body: str, vars: dict) -> str:
    # 1. Раскрытие spintax (изнутри наружу)
    text = body
    while True:
        new_text = SPIN_RE.sub(_choose_spin, text)
        if new_text == text:
            break
        text = new_text
    
    # 2. Подстановка переменных
    text = VAR_RE.sub(lambda m: vars.get(m.group(1), ''), text)
    
    # 3. Снятие экранирования
    text = text.replace(r'\{', '{').replace(r'\}', '}')
    return text

def _choose_spin(m):
    parts = m.group(1).split('|')
    return random.choice(parts) if len(parts) > 1 else m.group(0)
```

Регулярка `SPIN_RE` намеренно не пропускает `|` — внутри `{...|...}` без `|` это не spintax, а возможно неопознанная переменная. Цикл `while` обрабатывает вложенность.

### 8.4. Валидация при сохранении шаблона

При добавлении шаблона через бот происходит:
- Пробный рендер 5 раз → должны получаться валидные тексты длиной до 4096 символов (лимит Telegram).
- Парсинг — какие переменные требуются. Сохраняем в `templates.variables`.
- Проверка на «недозакрытые» скобки.

---

## 9. Очередь задач и атомарное распределение

### 9.1. Создание задач

При запуске кампании:
```python
async def create_tasks(campaign_id: int, usernames: list[str]):
    # 1. Нормализация
    usernames = [u.strip().lstrip('@').lower() for u in usernames if u.strip()]
    usernames = list(dict.fromkeys(usernames))  # дедуп с сохранением порядка
    
    # 2. Фильтрация по processed_clients
    cutoff = None if campaign.resend_old else datetime(1970, 1, 1)
    if campaign.resend_old:
        cutoff = now() - timedelta(days=180)
    
    skip = await db.fetch(
        "SELECT username FROM processed_clients WHERE last_processed_at >= $1",
        cutoff
    )
    skip_set = {row['username'] for row in skip}
    
    to_create = [u for u in usernames if u not in skip_set]
    skipped_count = len(usernames) - len(to_create)
    
    # 3. Bulk insert
    await db.executemany(
        "INSERT INTO tasks (campaign_id, username, status) VALUES ($1, $2, 'queued') "
        "ON CONFLICT (campaign_id, username) DO NOTHING",
        [(campaign_id, u) for u in to_create]
    )
    
    # 4. Обновление кампании
    await db.execute(
        "UPDATE campaigns SET total_count = $1, skipped_count = $2 WHERE id = $3",
        len(to_create), skipped_count, campaign_id
    )
```

### 9.2. Захват задачи воркером
См. SQL в разделе 6.2. Параллельные воркеры конкурируют за следующую задачу через `FOR UPDATE SKIP LOCKED`. Это гарантирует:
- Никакие два воркера не возьмут одну задачу.
- Если воркер занят/упал, его «нераспределённые» задачи (`status = queued`) подберут другие.
- Задачи с `locked_until > NOW()` пропускаются (отложенный retry после FloodWait).

### 9.3. Возврат задачи в очередь

Если воркер начал задачу, но не смог завершить из-за временной проблемы (FloodWait):
```sql
UPDATE tasks
SET status = 'queued',
    assigned_account_id = NULL,
    locked_until = NOW() + INTERVAL '<flood_seconds> seconds'
WHERE id = :task_id;
```

При перезапуске приложения все «зависшие» задачи (`status = in_progress` старше 1 часа) автоматически возвращаются в очередь:
```sql
UPDATE tasks
SET status = 'queued', assigned_account_id = NULL
WHERE status = 'in_progress'
  AND last_attempt_at < NOW() - INTERVAL '1 hour';
```

Это выполняется при старте `main.py`.

---

## 10. Управляющий Telegram-бот: сценарии и команды

### 10.1. Защита от посторонних

Middleware aiogram проверяет `message.from_user.id` против `ALLOWED_USER_IDS`. Все остальные апдейты молча игнорируются (без ответа, чтобы не палить факт существования бота).

```python
class AuthMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get('event_from_user')
        if user is None or user.id not in settings.allowed_user_ids:
            return  # молчим
        return await handler(event, data)
```

### 10.2. Команды

| Команда | Описание |
|---|---|
| `/start`, `/help` | Главное меню с inline-кнопками |
| `/accounts` | Список аккаунтов с статусами |
| `/add_account` | FSM: добавление нового userbot-аккаунта |
| `/remove_account <phone>` | Удалить аккаунт |
| `/templates` | Список шаблонов |
| `/new_template` | FSM: создать шаблон |
| `/del_template <name>` | Удалить шаблон |
| `/new_campaign` | FSM: создать и запустить кампанию |
| `/status` | Статус текущих кампаний и аккаунтов |
| `/pause` | Пауза активной кампании |
| `/resume` | Возобновление |
| `/stop` | Полная остановка кампании |
| `/spamcheck` | Принудительный SpamBot-чек всех аккаунтов |
| `/spamcheck @username` | Принудительный чек одного аккаунта |
| `/floodwait` | Аккаунты в FloodWait сейчас + счётчик за 24ч |
| `/export_log [today\|yesterday\|N]` | Выгрузить лог за период |
| `/export_report <campaign_id>` | CSV-отчёт по кампании |
| `/settings` | Текущие настройки |
| `/set <key> <value>` | Изменить настройку (с валидацией) |
| `/cancel` | Выйти из любого FSM |

**Единый формат сообщений.** Списки и сводки (`/accounts`, `/campaigns`, `/status`,
`/floodwait`, `/templates`) рендерятся через `app/bot/formatting.py` — карточки с эмодзи-
статусами, статусы аккаунтов/кампаний по-русски, числа с разделителем тысяч. Логика хендлеров
от форматирования отделена (formatting — чистые функции, покрыты `tests/test_formatting.py`).

### 10.3. FSM «Добавить аккаунт»

```
state: waiting_phone
    user: "+79991234567"
    bot: вызывает client.send_code_request(phone)
        → success: переход в waiting_code
        → error (PhoneNumberInvalid): сообщение об ошибке, возврат в waiting_phone
state: waiting_code
    user: "12345"
    bot: вызывает client.sign_in(phone, code)
        → success: переход в waiting_proxy
        → SessionPasswordNeededError: переход в waiting_2fa
        → PhoneCodeInvalidError: повторить ввод кода
state: waiting_2fa
    user: "myCloudPassword"
    bot: вызывает client.sign_in(password=...)
        → success: переход в waiting_proxy
        → PasswordHashInvalidError: повторить
state: waiting_proxy
    bot: "Указать прокси? Формат: socks5://user:pass@host:port или 'skip'"
    user: "skip" или "socks5://..."
    bot: проверяет прокси через подключение тестовое (опционально)
        → сохраняет аккаунт в БД со статусом 'warmup', warmup_until = now + 48h
        → warmup-подписка на каналы, пока клиент авторизован (§5.1, MVP-5)
        → запускает воркер (worker_pool.start_for) + регистрирует spamcheck-задачу:
          аккаунт греется СРАЗУ, без рестарта приложения
        → "Аккаунт @username добавлен, подписан на N каналов. Warmup до <дата>."
```

При каждом шаге бот может принять `/cancel` и сбросить FSM. Промежуточный код хранится в FSM-context (`aiogram` поддерживает Redis/Memory storage; мы используем `MemoryStorage` — для одного админа достаточно).

### 10.4. FSM «Создать и запустить кампанию»

```
1. /new_campaign
2. bot: "Тип кампании? [Рассылка | Инвайт]" (inline-keyboard)
3. user: выбор
4. bot: "Загрузите TXT-файл со списком username"
5. user: документ
6. bot: парсит, отвечает:
   "Найдено 1247 username, уникальных 1198, уже обработанных 89.
    К работе: 1109 username.
    [✓ Продолжить] [Переотправить тем, кому писали >6мес назад] [Отмена]"
7. user: выбор
8. Если рассылка:
       bot: "Выберите шаблон" (inline список templates)
   Если инвайт:
       bot: "Введите целевой чат (@username или ID)"
       bot: проверяет наличие у админ-аккаунтов прав на инвайт в этот чат
9. bot: "Кампания #N готова к старту: 1109 целей, ~5 рабочих аккаунтов, ETA ~14ч.
        [▶ Старт] [Отмена]"
10. user: Старт
11. campaign.status = 'running', воркеры просыпаются
```

### 10.5. Прогресс-уведомления

Раз в `PROGRESS_NOTIFY_INTERVAL_SEC` (1800 сек = 30 мин) Scheduler отправляет в управляющий бот сводку:

```
Кампания #5 (рассылка) — running
Прогресс: 234/1109 (21%)
Успешно: 198 | Пропущено: 28 | Ошибок: 8
Аккаунты: 4 active, 1 pause (FloodWait 8 мин)
ETA: ~9ч 30мин
```

При критических событиях (PeerFlood, dead аккаунт, ошибка БД) — немедленное уведомление.

Реализация (MVP-5): `progress_notify_job` тикает каждую минуту, но шлёт сводку не чаще `progress_notify_interval_sec` и ТОЛЬКО при наличии running-кампаний (в простое молчит). ETA — приблизительная (по средней скорости обработки с `started_at`). Доставка — через `notify_admin` всем `ALLOWED_USER_IDS`.

### 10.6. Кнопки и инлайн-навигация

```
[Главное меню]
├── 📋 Аккаунты ──► список с кнопками [⏸ pause] [▶ activate] [🗑 delete] [🔍 spamcheck]
├── 📝 Шаблоны ──► список с кнопками [📄 view] [🗑 delete] + [➕ создать]
├── 🚀 Новая кампания ──► FSM
├── 📊 Статус ──► отображение текущих кампаний, кнопки [⏸] [▶] [⏹]
├── 📦 Экспорт ──► [Логи 24ч] [Логи 7д] [Отчёт по кампании]
└── ⚙️ Настройки ──► список ключ-значение + редактирование
```

(Эмодзи не пишем в .md и в саму архитектуру, но в UI бота они допустимы для лучшей читаемости — это явно UI-слой.)

---

## 11. Конфигурация и переменные окружения

### 11.1. Файл `.env`

```env
# === Telegram API ===
TG_API_ID=34554902
TG_API_HASH=<секрет, в репо НЕ хранится>
BOT_TOKEN=<секрет, в репо НЕ хранится>

# === Admin ===
ALLOWED_USER_IDS=1051702577

# === Database ===
POSTGRES_HOST=db
POSTGRES_PORT=5432
POSTGRES_DB=outreach
POSTGRES_USER=outreach
POSTGRES_PASSWORD=<сгенерированный пароль>

# === Encryption (опционально, для шифрования секретных полей) ===
FERNET_KEY=<base64 32 bytes>

# === Runtime ===
TZ=Europe/Minsk
LOG_LEVEL=INFO
SESSIONS_PATH=/app/data/sessions
LOGS_PATH=/app/data/logs

# === Behaviour (можно переопределять через таблицу settings) ===
DAILY_DM_LIMIT_WARM=40
DAILY_INVITE_LIMIT_WARM=100
DAILY_DM_LIMIT_FRESH=10
DAILY_INVITE_LIMIT_FRESH=5
INTERVAL_MIN_SEC=300
INTERVAL_MAX_SEC=540
SPAMCHECK_INTERVAL_SEC=240
PROGRESS_NOTIFY_INTERVAL_SEC=1800
QUIET_HOURS_START=01:00
QUIET_HOURS_END=07:00
PEERFLOOD_LIMIT_RATIO=0.75
WARMUP_DURATION_HOURS=48
ADAPTIVE_LIMIT_REDUCTION_DAYS=7
```

В `.env.example` (коммитится в Git) все секреты заменены на `<placeholder>`. Реальный `.env` находится в `.gitignore`.

### 11.2. Pydantic settings

```python
# app/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', case_sensitive=False)
    
    tg_api_id: int
    tg_api_hash: str
    bot_token: str
    allowed_user_ids: list[int]
    
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str
    
    fernet_key: str | None = None
    
    tz: str = 'Europe/Minsk'
    log_level: str = 'INFO'
    sessions_path: str
    logs_path: str
    
    # Дефолты, реальные значения подтягиваются из таблицы settings
    daily_dm_limit_warm: int = 40
    daily_invite_limit_warm: int = 100
    # ...
    
    @property
    def database_url(self) -> str:
        return (
            f'postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}'
            f'@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}'
        )

settings = Settings()
```

---

## 12. Структура проекта

```
telegram-outreach/
├── docker-compose.yml
├── Dockerfile
├── .env                          # gitignored
├── .env.example
├── .gitignore
├── .dockerignore
├── pyproject.toml                # poetry / hatch + dependencies
├── alembic.ini
├── README.md
├── ARCHITECTURE.md               # этот документ
├── data/                         # gitignored, mounted as volume
│   ├── sessions/                 # .session файлы Telethon
│   └── logs/
├── migrations/                   # alembic migrations
│   ├── env.py
│   └── versions/
└── app/
    ├── __init__.py
    ├── main.py                   # entry point: запускает Bot + Manager + Scheduler
    ├── config.py                 # pydantic-settings
    ├── constants.py              # WARMUP_CHANNELS, статусы и т.д.
    │
    ├── db/
    │   ├── __init__.py
    │   ├── session.py            # AsyncSession factory
    │   ├── models.py             # SQLAlchemy declarative models
    │   └── repositories/
    │       ├── accounts.py
    │       ├── campaigns.py
    │       ├── tasks.py
    │       ├── templates.py
    │       ├── processed.py
    │       ├── settings.py
    │       └── logs.py
    │
    ├── telegram/                 # Telethon-слой (рабочие аккаунты)
    │   ├── __init__.py
    │   ├── client_factory.py     # создание Telethon-клиентов
    │   ├── auth.py               # FSM-шаги авторизации (вынесены сюда логически)
    │   ├── worker.py             # WorkerAccount: цикл воркера
    │   ├── humanize.py           # типинг, задержки, jitter
    │   ├── errors.py             # маппинг exceptions → result_code
    │   ├── spam_checker.py       # /start в @SpamBot + парсинг
    │   ├── invite.py             # invite-логика, проверка членства
    │   ├── peer_cache.py         # кэш access_hash
    │   └── warmup.py             # warmup-сценарий для новых аккаунтов
    │
    ├── campaigns/
    │   ├── __init__.py
    │   ├── manager.py            # запуск/остановка/пауза кампаний
    │   ├── template_engine.py    # spintax + переменные
    │   ├── progress.py           # агрегация прогресса
    │   └── reporting.py          # CSV-отчёты
    │
    ├── bot/                      # aiogram-слой (управляющий бот)
    │   ├── __init__.py
    │   ├── main.py               # Bot, Dispatcher, polling
    │   ├── middlewares.py        # AuthMiddleware
    │   ├── keyboards.py          # inline/reply keyboards
    │   ├── states.py             # FSM States
    │   └── handlers/
    │       ├── __init__.py
    │       ├── common.py         # /start, /help, /cancel
    │       ├── accounts.py       # /accounts, /add_account FSM
    │       ├── templates.py
    │       ├── campaigns.py      # /new_campaign FSM
    │       ├── status.py
    │       ├── settings.py
    │       └── export.py
    │
    ├── scheduler/
    │   ├── __init__.py
    │   └── jobs.py               # APScheduler-задачи
    │
    ├── notifications/
    │   ├── __init__.py
    │   └── admin.py              # отправка сообщений админу из любого места
    │
    └── utils/
        ├── __init__.py
        ├── logging.py            # настройка loguru + structlog
        ├── crypto.py             # Fernet шифрование
        ├── txt_parser.py         # парсинг загруженного TXT-файла
        ├── time.py               # quiet hours, тайм-зоны
        └── pubsub.py             # Postgres LISTEN/NOTIFY обёртка
```

---

## 13. Развёртывание (Docker, VPS)

### 13.1. Dockerfile

```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# System deps (для cryptography и т.д.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libssl-dev libffi-dev tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir poetry==1.8.3 \
 && poetry config virtualenvs.create false \
 && poetry install --no-root --only main

COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./

RUN mkdir -p /app/data/sessions /app/data/logs \
 && chmod 700 /app/data/sessions

CMD ["python", "-m", "app.main"]
```

### 13.2. docker-compose.yml

```yaml
version: '3.9'

services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      TZ: ${TZ}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5

  app:
    build: .
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
    env_file:
      - .env
    environment:
      POSTGRES_HOST: db
      POSTGRES_PORT: 5432
    volumes:
      - ./data/sessions:/app/data/sessions
      - ./data/logs:/app/data/logs

volumes:
  pgdata:
```

Порты наружу не пробрасываются. Бот сам подключается к Telegram через polling. PostgreSQL доступен только из app-контейнера через internal docker network.

### 13.3. Деплой на VPS

```bash
# Один раз:
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
git clone git@github.com:<user>/telegram-outreach.git
cd telegram-outreach
cp .env.example .env
nano .env  # заполнить секреты
mkdir -p data/sessions data/logs
chmod 700 data/sessions

# Запуск:
docker compose up -d

# Логи:
docker compose logs -f app

# Обновление:
git pull
docker compose build app
docker compose up -d
```

### 13.4. Минимальные требования к VPS
- 2 vCPU
- 2 GB RAM (Postgres + Python с 5-10 Telethon-клиентами)
- 20 GB SSD
- Любой стабильный Linux (Ubuntu 22.04 / 24.04, Debian 12 рекомендуются)
- Открытый исходящий трафик к Telegram DC (доступ к 149.154.0.0/16, 91.108.0.0/16 и MTProto-серверам)

---

## 14. Безопасность

### 14.1. Защита секретов

- `.env` в `.gitignore`, права `600`.
- `data/sessions/` — права `700`, файлы `.session` — `600`.
- Опциональное шифрование `proxy_url` и `tg_api_hash` в БД через Fernet (`FERNET_KEY` в `.env`).
- `BOT_TOKEN` известен только админу. При компрометации — `/revoke` в @BotFather, новый токен в `.env`, рестарт.

### 14.2. Авторизация управляющего бота

- Whitelist `ALLOWED_USER_IDS` фильтрует на уровне middleware.
- Все остальные апдейты молча игнорируются (бот не отвечает).
- При попытке постороннего войти — событие пишется в логи (event_type `unauthorized_access`).

### 14.3. Сетевая безопасность VPS

- Firewall (`ufw`): только SSH (22), всё остальное deny incoming.
- SSH только по ключам, root-логин запрещён.
- Postgres недоступен снаружи (только в docker network).

### 14.4. Защита от утечки сессий

- При компрометации VPS противник получит сессии и сможет управлять аккаунтами. Это известный риск любого userbot-софта.
- Митигация: регулярные бэкапы, шифрование диска (LUKS), мониторинг входов на VPS.
- При подозрении на компрометацию — `client.log_out()` для всех аккаунтов через бот-команду `/emergency_logout`.

---

## 15. Логирование

### 15.1. Что логируется

| Уровень | Событие | Где |
|---|---|---|
| INFO | account_added, account_status_changed | БД + файл |
| INFO | campaign_started, paused, resumed, finished | БД + файл |
| INFO | message_sent, invite_sent | БД + файл |
| INFO | task_skipped (с причиной) | БД + файл |
| INFO | spam_check_result (только при смене статуса) | БД + файл |
| INFO | limit_restored | БД + файл |
| WARNING | flood_wait | БД + файл |
| WARNING | unauthorized_access | БД + файл |
| ERROR | peer_flood | БД + файл (+ уведомление админу) |
| ERROR | account_dead | БД + файл (+ уведомление админу) |
| ERROR | task_failed (неожиданная ошибка) | БД + файл |
| CRITICAL | db_unavailable, telethon_disconnect | файл (+ уведомление админу при возможности) |

### 15.2. Что НЕ логируется

- Успешные RPC-вызовы без действия (resolve, get_entity, get_participants).
- Сам контент сообщения после spintax/render — фиксируется только в task.result, не разбрасывается по логу.
- Промежуточные heartbeat'ы воркера.

### 15.3. Конфигурация loguru

```python
from loguru import logger
logger.add(
    'data/logs/app_{time:YYYY-MM-DD}.log',
    rotation='1 day',
    retention='30 days',
    compression='zip',
    level='INFO',
    enqueue=True,           # async-safe
    serialize=False,        # human-readable, чтобы было удобно читать
)
```

Параллельно структурированные события идут в БД (`logs` table) для запросов из бота: «покажи всё по аккаунту X за день».

### 15.4. Выгрузка логов через бот

```
/export_log today        # сегодняшний .log файл
/export_log yesterday    # вчерашний
/export_log 7            # последние 7 дней (zip)
```

Бот отправляет файл документом.

---

## 16. Обработка ошибок Telethon

### 16.1. Маппинг exceptions → result_code

```python
# app/telegram/errors.py
from telethon.errors import (
    FloodWaitError, PeerFloodError,
    UserPrivacyRestrictedError, UserNotMutualContactError,
    UsernameInvalidError, UsernameNotOccupiedError,
    UserBannedInChannelError,
    ChatAdminRequiredError, UserAlreadyParticipantError,
    ChannelPrivateError, UsersTooMuchError,
    InputUserDeactivatedError, UserIsBlockedError,
    UserIsBotError, ChatWriteForbiddenError,
)

ERROR_MAP = {
    UserPrivacyRestrictedError: ('privacy_restricted', 'skip'),
    UserNotMutualContactError: ('not_mutual_contact', 'skip'),
    UsernameInvalidError: ('not_found', 'skip'),
    UsernameNotOccupiedError: ('not_found', 'skip'),
    UserBannedInChannelError: ('banned_in_channel', 'skip'),
    # UserDeactivatedError здесь НЕТ: это 401 (наш аккаунт удалён/забанен) →
    # SESSION_DEAD_ERRORS. InputUserDeactivatedError (код 400) — удалён получатель.
    InputUserDeactivatedError: ('deactivated', 'skip'),
    UserIsBlockedError: ('privacy_restricted', 'skip'),
    UserAlreadyParticipantError: ('already_member', 'skip'),
    ChannelPrivateError: ('channel_private', 'fatal'),
    UsersTooMuchError: ('too_many_channels', 'skip'),
    ChatAdminRequiredError: ('channel_private', 'fatal'),
    ChatWriteForbiddenError: ('privacy_restricted', 'skip'),
    UserIsBotError: ('not_found', 'skip'),
}
```

- `'skip'` — задача помечается failed с этим кодом, переходим к следующей.
- `'fatal'` — кампания останавливается (например, целевой чат недоступен для всех аков).
- `FloodWaitError` и `PeerFloodError` обрабатываются отдельно (см. раздел 6).

**Недействительная сессия в рантайме (MVP-5).** Ошибки уровня 401 (`UnauthorizedError`
и подклассы, в т.ч. `AuthKeyUnregisteredError` — «The key is not registered in the
system», а также `UserDeactivatedError`/`UserDeactivatedBanError` — наш аккаунт
удалён/забанен) означают, что сессия/аккаунт мертвы (разлогин, отзыв сессии, бан/деактивация).
Это особенно частая казнь свежих аккаунтов при ранней автоматизации (см. §5.1, §14.4).
Такие ошибки в воркере (действия) и в `spamcheck_job` ловятся как `SESSION_DEAD_ERRORS`
и **немедленно** переводят аккаунт в `dead` + уведомляют админа + останавливают воркер —
без них воркер «висел» бы на мёртвой сессии, а `spamcheck` шумел бы ошибкой каждые 4 минуты.

**Инвайт: per-account обработка ошибок прав (MVP-4, §5.3).** Задачи распределяются
через `SKIP LOCKED` любому воркеру, но пригласить может только аккаунт-участник
целевого чата с правом `invite_users`. Поэтому в invite-режиме `ChatAdminRequiredError`
и `ChannelPrivateError` трактуются **по-аккаунтно**, а не сразу `'fatal'`: аккаунт
помечается непригодным для этой кампании (in-memory), его задача возвращается в очередь
(`requeue`) и достаётся другому аккаунту; в `claim_next_task` такой аккаунт исключает эту
кампанию (`exclude_campaign_ids`). Кампания переходит в `paused` (`'fatal'`-аналог) только
когда непригодны **все** рабочие аккаунты. В message-режиме эти ошибки остаются `'fatal'`
(целевой чат недоступен в принципе). На этапе создания инвайт-кампании FSM резолвит чат и
проверяет право каждого аккаунта, показывая разбивку eligible/ineligible (§10.4).

### 16.2. Универсальный wrapper

```python
async def safe_telegram_action(coro, account_id, task_id):
    try:
        return await coro
    except FloodWaitError as e:
        await handle_flood_wait(account_id, task_id, e.seconds)
        raise
    except PeerFloodError:
        await handle_peer_flood(account_id, task_id)
        raise
    except tuple(ERROR_MAP.keys()) as e:
        result_code, severity = ERROR_MAP[type(e)]
        return TaskResult(code=result_code, severity=severity, error=str(e))
    except (ConnectionError, asyncio.TimeoutError) as e:
        # сетевые сбои — retry после паузы
        await asyncio.sleep(30)
        raise RetryableError(str(e))
```

---

## 17. План разработки по этапам

### MVP-1: Инфраструктура (3-4 дня)
- Каркас проекта (Dockerfile, docker-compose.yml, .env).
- Postgres + Alembic + базовая схема (только `accounts`, `settings`, `logs`).
- Pydantic-settings.
- Управляющий бот: `/start`, whitelist, `/add_account` FSM с 2FA.
- Сохранение `.session` на volume.
- Хранение и просмотр аккаунтов через `/accounts`.

**Критерий готовности:** можно добавить один аккаунт через бот, увидеть его в списке.

### MVP-2: Шаблоны и одиночная рассылка (3-4 дня)
- Таблицы `templates`, `campaigns`, `tasks`, `processed_clients`.
- Парсер TXT-файла.
- Spintax-движок.
- Команда `/new_template`, `/new_campaign` (только рассылка).
- Один воркер обрабатывает по очереди.

**Критерий готовности:** одна кампания на 5-10 username, реальная отправка с одного аккаунта.

### MVP-3: Multi-account + антибан (4-5 дней)
- Worker Pool: N параллельных воркеров.
- SKIP LOCKED для атомарной выдачи задач.
- Обработка FloodWait/PeerFlood с правильным state transition.
- SpamBot scheduler-задача каждые 4 минуты.
- Парсинг ответов SpamBot.
- Human-like: typing, задержки, jitter.
- Quiet hours.

**Критерий готовности:** 3-5 аккаунтов параллельно обрабатывают список 100 username с реалистичными интервалами.

### MVP-4: Invite-режим (2-3 дня)
- Реализация invite-логики.
- Кэш participants для проверки членства.
- Маппинг invite-специфичных ошибок.
- FSM `/new_campaign` теперь поддерживает оба типа.

**Критерий готовности:** инвайт-кампания на тестовом чате работает корректно.

### MVP-5: Warmup и адаптивные лимиты (2-3 дня)
- Warmup-сценарий для новых аккаунтов.
- Подписка на каналы при добавлении.
- Адаптивный лимит 75% после PeerFlood.
- Возврат к 100% после ok от SpamBot.
- Прогресс-уведомления раз в 30 мин в бот.

### MVP-6: Стабилизация и эксплуатация (2-3 дня)
- CSV-отчёты по кампаниям.
- Команды `/export_log`, `/export_report`.
- Все формальные edge-кейсы из раздела 16.
- Документация по эксплуатации (README).
- Скрипт бэкапа БД и сессий.

**Итого ≈ 17-22 рабочих дня.**

---

## 18. Эксплуатация и резервное копирование

### 18.1. Бэкап

Cron на VPS, ежедневно в 03:00:

```bash
#!/bin/bash
# /opt/telegram-outreach/backup.sh
set -e
TS=$(date +%Y%m%d_%H%M)
BACKUP_DIR=/var/backups/telegram-outreach
mkdir -p $BACKUP_DIR

# 1. БД
docker compose exec -T db pg_dump -U outreach outreach \
    | gzip > $BACKUP_DIR/db_$TS.sql.gz

# 2. Сессии
tar czf $BACKUP_DIR/sessions_$TS.tar.gz data/sessions

# 3. Удалить старые (>14 дней)
find $BACKUP_DIR -type f -mtime +14 -delete
```

Опционально — синк в S3-совместимое хранилище (Backblaze B2, Hetzner Storage Box). Включается флагом в v1.1.

### 18.2. Восстановление

```bash
# БД
gunzip -c db_<ts>.sql.gz | docker compose exec -T db psql -U outreach outreach

# Сессии
tar xzf sessions_<ts>.tar.gz
```

### 18.3. Мониторинг

В v1 — никаких внешних систем (Prometheus/Grafana). Достаточно:
- `docker compose ps` для статуса контейнеров.
- `docker compose logs --tail=200 app` для текущих логов.
- Бот сам уведомляет о критических событиях.

В v1.1 можно добавить healthcheck-endpoint и подключить uptimerobot или аналог.

### 18.4. Рестарт после падения

`restart: unless-stopped` в docker-compose обеспечивает автоперезапуск. При старте:
1. App ждёт healthcheck Postgres.
2. Alembic накатывает миграции (если новые).
3. Подписывается на `pg_notify('settings_changed')`.
4. Загружает аккаунты из БД, фильтрует `disabled`/`dead`.
5. Поднимает воркеры по одному с offset.
6. Все задачи `in_progress` старше 1 часа возвращаются в `queued`.
7. Все кампании в статусе `running` подхватываются с текущего места.
8. Запускает APScheduler.
9. Запускает aiogram polling.

Время от падения до полного восстановления — обычно 10-30 секунд.

---

## 19. Расширения за пределами v1

Возможные доработки, которые **не** входят в v1, но архитектура их допускает:

1. **Read-only веб-дашборд** (FastAPI + HTMX + Tailwind) для удобного просмотра больших логов и статистики.
2. **Прокси-пул**: отдельная таблица `proxies`, ротация при ошибках, автоматическая проверка.
3. **Авто-бэкап в S3**.
4. **Spam-фильтр входящих сообщений на userbot**: автоудаление спам-ответов от ботов.
5. **Анти-капча на этапе авторизации**: если Telegram попросит решить капчу при добавлении аккаунта.
6. **Расписание кампаний**: запуск в заданное время, отложенный старт.
7. **A/B-тестирование шаблонов**: разные шаблоны разным группам, сравнение отклика.
8. **Импорт списка из CSV/Excel** с дополнительными переменными.
9. **Webhook вместо polling** для управляющего бота (требует TLS на VPS).
10. **Многопользовательский режим**: разные админы видят свои аккаунты и кампании.
11. **DB-снимок кэша participants** (§5.2): сохранять members целевых чатов в Postgres, чтобы инвайт-кампания не перезаполняла кэш после рестарта. В MVP-4 кэш только in-memory.

---

## Приложение А. Глоссарий

| Термин | Значение |
|---|---|
| Userbot / userbot-аккаунт | Обычный пользовательский аккаунт Telegram, управляемый программно через MTProto |
| Управляющий бот | Telegram-бот (созданный в @BotFather) для управления софтом |
| Кампания | Одна логическая операция «разослать сообщение / пригласить список» с конкретным списком и шаблоном |
| Задача (task) | Один username в рамках кампании |
| Воркер | Асинхронная задача, обслуживающая один userbot-аккаунт |
| FloodWait | Временное ограничение Telegram: «подожди N секунд» |
| PeerFlood | Жёсткое ограничение: «вы шлёте слишком многим неконтактам» |
| SpamBot | Системный бот Telegram (@SpamBot) для проверки статуса аккаунта |
| Warmup | Начальный период (24-48ч) щадящей активности для свежего аккаунта |
| Spintax | Синтаксис `{a|b|c}` для случайного выбора варианта текста |
| Quiet hours | Окно тишины ночью, когда воркеры не работают |
| SKIP LOCKED | SQL-механизм Postgres для атомарной выдачи задач параллельным воркерам без блокировок |

---

## Приложение Б. Источники и документация

- Telethon: https://docs.telethon.dev/
- Telethon errors reference: https://docs.telethon.dev/en/stable/quick-references/events-reference.html
- Telegram API (MTProto): https://core.telegram.org/api
- Telegram Bot API: https://core.telegram.org/bots/api
- aiogram 3: https://docs.aiogram.dev/
- SQLAlchemy 2.0: https://docs.sqlalchemy.org/en/20/
- Alembic: https://alembic.sqlalchemy.org/
- APScheduler: https://apscheduler.readthedocs.io/
- PostgreSQL SKIP LOCKED: https://www.postgresql.org/docs/16/sql-select.html#SQL-FOR-UPDATE-SHARE
- @SpamBot: https://t.me/spambot

---

Конец документа.
