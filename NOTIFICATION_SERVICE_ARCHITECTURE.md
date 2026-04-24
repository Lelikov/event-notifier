> **ARCHIVED (2026-04-20):** This document describes an earlier design that was never implemented. The current architecture is documented in `docs/architecture/services/event-notifier/SERVICE_OVERVIEW.md`. Do not use this document as a reference for the current system.

# Notification Service — архитектура и контракт интеграции

## Назначение

Notification Service — микросервис, ответственный за доставку уведомлений пользователям по различным каналам (email, Telegram, push-уведомления, WhatsApp). Сервис не содержит бизнес-логики — он получает доменные события через RabbitMQ в формате CloudEvents и преобразует их в уведомления для конечных получателей.

Сервис обслуживает платформу, где волонтёры проводят встречи с клиентами. Уведомления охватывают жизненный цикл встреч, обратную связь и пользовательские действия.

---

## Общая схема взаимодействия

```
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Meeting Svc  │  │ Feedback Svc │  │  Other Svc   │
└──────┬───────┘  └──────┬───────┘  └──────┬───────┘
       │                 │                 │
       │   CloudEvents   │                 │
       └────────┬────────┘─────────────────┘
                ▼
         ┌─────────────┐
         │  RabbitMQ    │
         │  (очередь)   │
         └──────┬──────┘
                ▼
  ┌─────────────────────────────────────────┐
  │       Notification Service              │
  │                                         │
  │  ┌─────────────┐   ┌────────────────┐  │
  │  │   Event      │   │   Outbox       │  │
  │  │   Consumer   │──▶│   Sender       │  │
  │  └─────────────┘   └───────┬────────┘  │
  │         │                   │           │
  │    PostgreSQL          Channels         │
  └─────────────────────────────────────────┘
                                │
                  ┌─────────────┼──────────────┐
                  ▼             ▼              ▼
              Email        Telegram         Push
           (внешний       (Bot API)        (FCM)
           шаблонизатор)
```

---

## Формат событий: CloudEvents

Все сервисы-источники публикуют события в RabbitMQ в формате CloudEvents v1.0. Notification Service потребляет очередь `notifications.events`.

### Обязательная структура сообщения

```json
{
  "specversion": "1.0",
  "type": "meeting.created",
  "source": "/meeting-service",
  "id": "evt-unique-uuid",
  "subject": "meeting-7890",
  "datacontenttype": "application/json",
  "data": {
    // payload, зависит от типа события
  }
}
```

| Поле | Описание |
|------|----------|
| `type` | Тип события. Определяет маршрутизацию, выбор шаблона и получателей. |
| `source` | Идентификатор сервиса-источника. Используется для трассировки. |
| `id` | Уникальный идентификатор события. Используется для идемпотентности — повторное событие с тем же `id` будет проигнорировано. |
| `subject` | ID сущности, к которой относится событие (опционально). |
| `data` | Payload события. Должен содержать ID участников и данные для шаблонов. |

### Важно для сервисов-источников

- Публикуйте **одно событие на один доменный факт**. Не дублируйте событие для каждого получателя — Notification Service сам выполняет fan-out.
- Поле `id` должно быть глобально уникальным (UUID v4). Повторная доставка сообщения с тем же `id` будет пропущена.
- В `data` обязательно включайте ID всех участников события (см. раздел «Маршрутизация»).

---

## Зарегистрированные типы событий

| Тип события | Описание | Обязательные поля в `data` |
|-------------|----------|---------------------------|
| `meeting.created` | Новая встреча назначена | `meeting_id`, `title`, `datetime`, `location`, `volunteer_id`, `client_id` |
| `meeting.updated` | Изменены параметры встречи | `meeting_id`, `title`, `datetime`, `location`, `volunteer_id`, `client_id`, `changes` (объект с изменёнными полями) |
| `meeting.cancelled` | Встреча отменена | `meeting_id`, `title`, `datetime`, `volunteer_id`, `client_id`, `reason` |
| `meeting.reminder` | Напоминание о скорой встрече | `meeting_id`, `title`, `datetime`, `location`, `volunteer_id`, `client_id`, `minutes_before` |
| `feedback.requested` | Запрос обратной связи от клиента | `meeting_id`, `meeting_title`, `client_id`, `feedback_link` |
| `feedback.submitted` | Обратная связь получена | `meeting_id`, `meeting_title`, `volunteer_id`, `client_id`, `rating` |
| `user.action.joined` | Пользователь присоединился | `meeting_id`, `user_id`, `user_role` |
| `user.action.left` | Пользователь покинул встречу | `meeting_id`, `user_id`, `user_role` |

### Пример: meeting.created

```json
{
  "specversion": "1.0",
  "type": "meeting.created",
  "source": "/meeting-service",
  "id": "evt-a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "subject": "meeting-7890",
  "datacontenttype": "application/json",
  "data": {
    "meeting_id": "7890",
    "title": "Консультация по документам",
    "datetime": "2026-04-15T14:00:00+03:00",
    "location": "ул. Пушкина, 10, каб. 3",
    "duration_minutes": 60,
    "volunteer_id": "550e8400-e29b-41d4-a716-446655440001",
    "client_id": "550e8400-e29b-41d4-a716-446655440002",
    "created_by": "550e8400-e29b-41d4-a716-446655440001"
  }
}
```

### Пример: meeting.cancelled

```json
{
  "specversion": "1.0",
  "type": "meeting.cancelled",
  "source": "/meeting-service",
  "id": "evt-cancel-uuid",
  "subject": "meeting-7890",
  "data": {
    "meeting_id": "7890",
    "title": "Консультация по документам",
    "datetime": "2026-04-15T14:00:00+03:00",
    "volunteer_id": "550e8400-e29b-41d4-a716-446655440001",
    "client_id": "550e8400-e29b-41d4-a716-446655440002",
    "reason": "Волонтёр заболел",
    "cancelled_by": "550e8400-e29b-41d4-a716-446655440001"
  }
}
```

---

## Маршрутизация: кто получает уведомления

Notification Service **не** определяет получателей по коду. Маршрутизация управляется таблицей `routing_rules`:

| event_type | recipient_field | recipient_role | priority | ignore_quiet_hours |
|------------|----------------|----------------|----------|--------------------|
| `meeting.created` | `client_id` | `client` | normal | false |
| `meeting.created` | `volunteer_id` | `volunteer` | normal | false |
| `meeting.cancelled` | `client_id` | `client` | urgent | true |
| `meeting.cancelled` | `volunteer_id` | `volunteer` | urgent | true |
| `feedback.requested` | `client_id` | `client` | normal | false |
| `feedback.submitted` | `volunteer_id` | `volunteer` | normal | false |

`recipient_field` указывает, из какого поля `data` извлечь UUID пользователя. Одно событие может породить уведомления нескольким получателям.

Для добавления нового типа получателя (например, «координатор») достаточно добавить строку в `routing_rules` и шаблон в `notification_templates` — без изменений кода.

---

## Каналы доставки

| Канал | Механизм | Шаблонизация |
|-------|----------|-------------|
| **Email** | Внешний сервис (SendGrid / Mailgun / кастомный). Notification Service передаёт `template_id` + контекст. | Внешняя. HTML-шаблоны хранятся и рендерятся на стороне email-сервиса. |
| **Telegram** | Telegram Bot API. Отправка через бот. | Локальная. Jinja2-шаблоны в БД Notification Service. |
| **Push** | Firebase Cloud Messaging (FCM). | Локальная. Jinja2-шаблоны в БД Notification Service. |
| **WhatsApp** | Twilio / WhatsApp Business API. | Локальная. Jinja2-шаблоны в БД Notification Service. |

### Email: взаимодействие с внешним шаблонизатором

Notification Service **не рендерит** HTML-письма. Вместо этого:

1. В таблице `notification_templates` для email-записей хранится `external_template_id` (например, `"d-meeting-created-client-ru"` для SendGrid).
2. При обработке события consumer находит `external_template_id` и кладёт его в outbox вместе с сырым контекстом (JSON).
3. Sender при отправке передаёт `template_id` + `context` в API email-сервиса.

Контекст, передаваемый во внешний шаблонизатор, содержит те же переменные, что и локальные шаблоны:

```json
{
  "recipient": {
    "first_name": "Мария",
    "last_name": "Иванова",
    "email": "maria@example.com"
  },
  "title": "Консультация по документам",
  "datetime": "2026-04-15T14:00:00+03:00",
  "location": "ул. Пушкина, 10, каб. 3",
  "meeting_id": "7890",
  "volunteer_id": "...",
  "client_id": "..."
}
```

---

## Хранение данных

### Что хранит Notification Service (PostgreSQL)

| Таблица | Назначение |
|---------|-----------|
| `notification_templates` | Шаблоны для Telegram/push (Jinja2) и ссылки на внешние email-шаблоны (`external_template_id`). |
| `routing_rules` | Правила маршрутизации: какой тип события → каким ролям. |
| `notification_preferences` | Предпочтения пользователей: каналы, тихие часы, режим дайджеста. |
| `notification_outbox` | Transactional outbox: уведомления, ожидающие отправки. |
| `delivery_log` | Журнал доставки: статусы, ошибки, timestamps попыток. |
| `device_tokens` | FCM/APNs токены для push, Telegram chat_id, email-адреса. |
| `processed_events` | Идемпотентность: ID обработанных CloudEvents (TTL 7 дней). |

### Что НЕ хранит Notification Service

- **Профили пользователей** — запрашиваются из User Profile Service по HTTP/gRPC. Кешируются в памяти (TTL 5 минут).
- **Бизнес-данные встреч** — приходят внутри CloudEvent payload. Notification Service не хранит их дольше, чем нужно для рендеринга.

---

## Зависимости от внешних сервисов

### User Profile Service

Notification Service запрашивает профили пользователей для получения контактных данных и роли.

**Ожидаемый контракт** (GET `/users/{user_id}`):

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440001",
  "role": "volunteer",
  "first_name": "Иван",
  "last_name": "Петров",
  "email": "ivan@example.com",
  "phone": "+79001234567",
  "telegram_chat_id": "123456789",
  "locale": "ru"
}
```

При недоступности User Profile Service — используется кеш (stale data). Если кеша нет — сообщение остаётся в RabbitMQ (nack + requeue).

---

## Архитектура процесса

Notification Service — **один процесс** с двумя внутренними компонентами:

### Event Consumer

Слушает RabbitMQ → парсит CloudEvent → проверяет идемпотентность → определяет получателей по routing_rules → загружает профили из User Profile Service → рендерит шаблоны (или подготавливает external_template_id для email) → записывает в outbox + processed_events **в одной транзакции** → ACK в RabbitMQ.

### Outbox Sender

Фоновый цикл (poll раз в секунду) → `SELECT ... FOR UPDATE SKIP LOCKED` из outbox → отправка через channel adapter → обновление статуса.

Два компонента работают как параллельные asyncio-задачи в одном Python-процессе. При нагрузке 20–30 уведомлений в минуту этого достаточно.

---

## Гарантии доставки

| Механизм | Что гарантирует |
|----------|----------------|
| **Transactional Outbox** | Запись в outbox и в processed_events — одна транзакция. Если транзакция упала, RabbitMQ доставит событие повторно. Если транзакция прошла, уведомление гарантированно в outbox. |
| **Идемпотентность** | CloudEvent.id используется как ключ. Повторная обработка одного события невозможна. |
| **Retry с backoff** | При ошибке отправки — экспоненциальный backoff (10с, 40с, 90с, 160с, 250с). До 5 попыток. |
| **Dead letter** | После исчерпания retry — статус `failed`, запись в delivery_log. Мониторинг + алерт. |
| **FOR UPDATE SKIP LOCKED** | Позволяет запускать несколько инстансов sender'а без двойных отправок. |

---

## Notification Preferences

Пользователи могут настраивать предпочтения по уведомлениям:

- **Каналы по типу события**: включить/выключить email, Telegram, push для конкретного типа события или для всех (`*`).
- **Тихие часы**: время + таймзона. Уведомления, попадающие в тихие часы, откладываются до их окончания.
- **Urgent-события** (например, `meeting.cancelled` за час до встречи) — **игнорируют** тихие часы.
- **Режим дайджеста**: `instant` (мгновенно) или `daily` (агрегация за день).

Дефолты при отсутствии настроек:
- Клиенты: push + email.
- Волонтёры: Telegram + push.

---

## Как добавить новый тип события

1. **Сервис-источник** публикует CloudEvent с новым `type` и нужными полями в `data`.
2. **Routing rules**: добавить строки в `routing_rules` (какие роли получают уведомление).
3. **Шаблоны**: добавить записи в `notification_templates` — для email: `external_template_id`, для Telegram/push: Jinja2-тело.
4. **Внешний email-сервис**: создать HTML-шаблон с соответствующим ID.

Изменения кода Notification Service **не требуются**.

---

## Как добавить новый канал доставки

1. Реализовать `ChannelAdapter` — класс с методами `send()` и опционально `send_template()`.
2. Зарегистрировать его в `build_adapters()`.
3. Добавить шаблоны для нового канала в `notification_templates`.
4. Обновить `_resolve_address()` в consumer для получения адреса из профиля.

---

## Структура outbox-записи

Outbox поддерживает два режима: локально отрендеренное тело или ссылку на внешний шаблон.

```
notification_outbox
├── id                    UUID PK
├── idempotency_key       TEXT UNIQUE  -- "{cloud_event_id}:{user_id}:{channel}"
├── user_id               UUID
├── recipient_address     TEXT         -- email / chat_id / device token
├── event_type            TEXT
├── channel               TEXT         -- "email" | "telegram" | "push" | "whatsapp"
├── subject               TEXT?        -- тема (для push/email fallback)
├── rendered_body         TEXT?        -- готовое тело (Telegram, push)
├── external_template_id  TEXT?        -- ID внешнего шаблона (email)
├── template_context      JSONB?       -- контекст для внешнего шаблона
├── status                TEXT         -- "pending" | "delivered" | "failed"
├── retry_count           INT
├── max_retries           INT
├── scheduled_at          TIMESTAMPTZ
├── created_at            TIMESTAMPTZ
└── updated_at            TIMESTAMPTZ

CONSTRAINT: rendered_body IS NOT NULL OR external_template_id IS NOT NULL
```

---

## Стек

| Компонент | Технология |
|-----------|-----------|
| Язык | Python 3.12+, asyncio |
| Message broker | RabbitMQ (aio-pika) |
| База данных | PostgreSQL (asyncpg) |
| Шаблоны (Telegram/push) | Jinja2 |
| Шаблоны (email) | Внешний сервис (SendGrid / Mailgun / кастомный) |
| HTTP-клиент | aiohttp |
| Конфигурация | pydantic-settings (env-переменные, префикс `NOTIFY_`) |
