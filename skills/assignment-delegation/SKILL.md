---
name: assignment-delegation
description: "Use send_assignment when the user asks for an external action that the agent cannot perform with available tools. If the send_assignment tool is absent, report that assignment delivery is not available; do not claim anything was sent."
version: 1.0.0
type: extension
runtime: python3
entry: plugin.py
permissions: [tool, net, read_settings]
env_from_settings:
  - ASSISTANT_ASSIGNMENT_SMTP_HOST
  - ASSISTANT_ASSIGNMENT_SMTP_PORT
  - ASSISTANT_ASSIGNMENT_SMTP_USERNAME
  - ASSISTANT_ASSIGNMENT_SMTP_PASSWORD
  - ASSISTANT_ASSIGNMENT_MAIL_FROM
  - ASSISTANT_ASSIGNMENT_MAIL_TO
  - ASSISTANT_ASSIGNMENT_SMTP_STARTTLS_REQUIRED
  - ASSISTANT_ASSIGNMENT_SMTP_ROOT_CERTIFICATES
  - ASSISTANT_ASSIGNMENT_SMTP_CLIENT_CERTIFICATE
  - ASSISTANT_ASSIGNMENT_SMTP_TLS_VERSION
  - ASSISTANT_ASSIGNMENT_SMTP_VERIFY_TLS
  - ASSISTANT_ASSIGNMENT_SMTP_TIMEOUT_SEC
  - ASSISTANT_ASSIGNMENT_MAX_ATTEMPTS
  - ASSISTANT_ASSIGNMENT_RETRY_MIN_SECONDS
  - ASSISTANT_ASSIGNMENT_RETRY_MAX_SECONDS
  - ASSISTANT_ASSIGNMENT_DELIVERY_TIMEOUT_SEC
  - DIALOGS_GRPC_ENDPOINT
  - ASSISTANT_DIALOGS_BOT_TOKEN
  - ASSISTANT_DIALOGS_BOT_GROUP_ID
  - DIALOGS_SECURE
  - DIALOGS_ROOT_CERTIFICATES
when_to_use: The user asks to send, schedule, book, reserve, buy, call, arrange, agree, request, calendar, meeting, or otherwise make someone else do an external-world action and no direct tool can perform it; Russian equivalents include календарь, встреча, забронировать, поручить.
---

# Assignment Delegation

Use this skill when the user asks for an action in the external world and the current tools cannot perform that action directly.

Examples:

- "Отправь письмо Иванову" means send an assignment to send that letter, unless an actual email-sending tool is available.
- "Поставь встречу с Ивановым" means send an assignment to schedule the meeting, unless a calendar tool is available.
- "Назначь созвон", "договорись о встрече", "запиши в календарь" also mean `send_assignment`.
- "Поставь в календарь встречу с Ивановым" means `send_assignment`; there is no separate calendar tool.
- "Забронируй столик" means send an assignment to make the reservation, unless a booking tool is available.

Decision rules:

1. If an available tool can really complete the requested action, use that tool.
2. If no available tool can complete the requested external action, use `send_assignment`.
3. For clear meeting/calendar/email/booking/purchase triggers, do not explain or decline; call `send_assignment`.
4. If it is unclear whether the user wants delegation or wants the agent to solve something directly, ask whether to send an assignment or try to solve it in the current chat.
5. Если без недостающих деталей поручение будет неоднозначным или рискованным, уточни только эти детали.
6. If the user's wording is already a valid assignment, send it as is.
7. If the tool reports partial delivery, tell the user that only part of the delivery succeeded and do not call `send_assignment` again automatically. Some targets may already have received the assignment; wait for explicit user instructions.

When composing `send_assignment`:

- Не додумывай детали.
- Не угадывай повестку встречи, список участников, адресатов, место, дату, время, ресторан, бюджет, текст письма, тон или приоритет.
- Если запрос уже понятен как поручение, отправь его как есть.
- Do not turn a short request into a richer plan unless the user supplied those details.
- Prefer preserving the user's original wording in the body.
- The subject should be short and describe the requested action.
- The body should include only user-provided facts and explicit unresolved gaps when useful.
- Do not mention delivery channels, recipients, mailboxes, chats, configuration, or internal routing.
