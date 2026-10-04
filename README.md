# Codex ↔ ChatGPT Bridge: macOS, несколько проектов

Адаптация [Zhenyu98/codex-chatgpt-bridge](https://github.com/Zhenyu98/codex-chatgpt-bridge), исходный commit `351c66fef0390872443af1587a978e6f76f479b8`. MIT-лицензия сохранена. Оригинальные Windows-скрипты оставлены в `skills/.../scripts`; macOS использует новый Python-контроллер.

Один bridge обслуживает независимые проекты. ChatGPT проектирует и принимает работу; Codex меняет код и запускает тесты. Проекты не могут иметь одинаковые или вложенные рабочие папки. Один основной Architect thread назначается одному проекту.

## Что реализовано и что требует подключения

Реализованы Streamable HTTP MCP, OAuth 2.1/PKCE, изоляция по project scope, безопасное чтение исходников, очередь TASK → Codex → result → ACCEPT/FIX, macOS Keychain, два LaunchAgent одного bridge (MCP и исполнитель), восстановление состояния и явный Off.

Пример профиля iiko: `config/iiko-backoffice.example.json`. Скопируйте его в `config/projects/iiko-backoffice.local.json` и заполните собственные пути и ID чатов. Исполнитель запускает отдельные короткие `codex exec`-сессии в рабочей папке; он не внедряет сообщения в существующий desktop Codex-чат.

Windows: исходный PowerShell/DevSpace workflow описан в [README.Windows.md](README.Windows.md). Принудительный read-only MCP этого fork реализован для macOS; Windows legacy-профиль использует разрешения как политику и не предоставляет ту же техническую границу.

**Автоматическая передача данных и автоматическое пробуждение ChatGPT — разные возможности.** MCP передаёт задания/результаты без ZIP и копирования файлов, когда Architect обращается к инструментам. Сам сервер не может заставить существующий ChatGPT-чат начать новый ответ. `chatgpt_thread` хранит привязку для маршрутизации, но не является API для отправки сообщений. Постоянный автономный цикл с пробуждением требует отдельно разрешённого транспорта/доступного механизма уведомлений. Cookies, скрытые ChatGPT API и пароль от аккаунта здесь не используются.

Для облачного ChatGPT потребуется HTTPS endpoint или Secure MCP Tunnel и подключение в интерфейсе ChatGPT. Текущий `http://127.0.0.1:8765/mcp` предназначен для локальной проверки. Авторизация конкретного чата и публичный туннель не появляются от одного сохранения ID в config.

`auto_execute` в примерах — `false`: сначала подключите Architect и проверьте рабочее дерево. Существующие задания продукта не импортируются автоматически. Для продолжения нумерации задайте `task_number_start`; исходное задание нужно передать через `submit_task` целиком, а не восстанавливать по названию.

## Установка

```sh
git clone https://github.com/DmitriySergeyev88/codex-chatgpt-bridge-win-macos.git
cd codex-chatgpt-bridge-win-macos
```

Нужны macOS, Python 3.11+, Git, Codex CLI с выполненным `codex login` и доступ к login Keychain. Проверенная версия Codex CLI — 0.160.0. При существующей авторизации через ChatGPT новый API key не требуется.

```sh
cd /absolute/path/to/codex-chatgpt-bridge
cp config/instance.example.json config/instance.local.json
mkdir -p config/projects
cp config/project.example.json config/projects/my-project.local.json
# Отредактируйте оба JSON: реальные абсолютные пути, ID Architect, URL и codex_binary.
./install.sh
.venv/bin/python -m bridge.cli launchd-install
.venv/bin/python -m bridge.cli on
.venv/bin/python -m bridge.cli doctor
```

Личные конфиги не входят в публичный репозиторий. `install.sh` создаёт venv и использует зафиксированные версии из `requirements.lock`. Он не меняет рабочие исходники проекта. `launchd-install` устанавливает два файла `org.codex.ai-bridge.server.plist` и `org.codex.ai-bridge.executor.plist` в `~/Library/LaunchAgents`. Они запускаются при входе пользователя и перезапускаются при сбое. Намеренный Off хранится отдельно; при следующем входе агенты не открывают выключенный bridge.

Мастер-ключ шифрования OAuth-state и пароль владельца хранятся в Keychain service `org.codex.ai-bridge`, accounts `<instance_id>:encryption` и `<instance_id>:owner`. В config нет паролей. Секреты не передаются как аргументы subprocess. Доступ к Keychain может запросить системное разрешение для Python.

## Подключение ChatGPT

1. Подготовьте стабильный HTTPS-прокси/туннель к `127.0.0.1:8765`. Он должен сохранять путь `/mcp`, OAuth-маршруты и корректный Host. Укажите HTTPS origin **без пути** в `public_base_url`, затем выполните `reboot`. Не направляйте прокси на DevSpace.
2. В developer mode ChatGPT добавьте MCP-подключение с URL `https://your-host/mcp`, выберите OAuth. SDK публикует discovery metadata, dynamic registration, authorize/token/revoke endpoints.
3. Для пароля владельца выполните **в собственном локальном интерактивном терминале** `.venv/bin/python -m bridge.cli owner-password`. Введите пароль только в странице авторизации bridge. Не вставляйте его в чат.
4. При OAuth выберите ровно один проект. Для другого проекта создайте отдельное подключение к тому же bridge и выберите другой project scope. Один сервер обслуживает все подключения; токен одного проекта не открывает другой.
5. В главном Architect-чате включите соответствующий MCP и вызовите `project_info`, `list_files`, `read_file` для README. `.env` и чужой проект должны возвращать ошибку. Затем включите локально `auto_execute: true` в профиле проекта и выполните `reboot`, когда очередь и рабочая папка готовы.

При наличии OpenAI Secure MCP Tunnel можно использовать поддерживаемый OpenAI tunnel-client для доступа к локальному `/mcp`. Это отдельный транспорт: его tunnel ID, runtime credential и авторизация не поставляются с репозиторием. Совместимость конкретного tunnel auth flow нужно проверить после подключения; локальные тесты не подтверждают её.

Официальная документация: [MCP server quickstart](https://developers.openai.com/plugins/build/app-quickstart), [подключение MCP](https://developers.openai.com/plugins/deploy/connect-chatgpt), [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels), [Codex non-interactive](https://learn.chatgpt.com/docs/non-interactive-mode).

## Профиль проекта

```json
{
  "bridge_project_id": "OSKZ-IIKO-BACKOFFICE",
  "name": "Сервис AI бэк-офиса iiko",
  "workspace": "/absolute/path/to/iiko",
  "chatgpt_thread": "architect-conversation-id",
  "codex_workspace": "/absolute/path/to/iiko",
  "mode": "CHATGPT_ARCHITECT",
  "chatgpt_access": "READ_ONLY",
  "auto_execute": false,
  "enabled": true,
  "task_number_start": 3,
  "deny_paths": ["ops", "config", "docs/test-results"]
}
```

В первой версии `workspace` и `codex_workspace` должны разрешаться в одну папку — так очередь, исходники и результат не расходятся. Это корень Git-репозитория, не весь домашний каталог. `bridge_project_id`: 3–64 символа, заглавные латинские буквы, цифры, `_` и `-`, первый символ — буква. `deny_paths` дополняет встроенный запрет секретов.

`chatgpt_thread` — назначенный основной чат. MCP не сообщает надёжный conversation ID: сервер принудительно изолирует **проект по OAuth scope**, но не может доказать, что вызов пришёл именно из записанного чата. Не включайте проектное подключение в другие чаты, которым этот проект не нужен.

При `init` создаётся:

```text
.ai-bridge/
├── state.json             # читаемая проекция состояния
├── architecture.md        # привязка ролей; действующие спецификации продукта остаются главными
├── tasks/TASK-NNN-Rn.json
├── results/TASK-NNN-Rn.json
├── reviews/TASK-NNN-Rn.json
├── queue.sqlite3          # источник истины и история переходов
├── .executor.lock
└── .export.lock
```

MCP не предоставляет произвольную запись файлов, включая `architecture.md`. Этот документ редактирует Codex локально по принятому решению. Для задач/ревью разрешены только структурированные операции, которые записывают фиксированные записи очереди. `READ_ONLY` означает read-only **исходников**, а не полный запрет metadata-записей.

## Цикл работы

Architect вызывает `submit_task(bridge_project_id, title, instructions, acceptance, idempotency_key)`. Инструкции должны включать полный scope и требования; недоступные ChatGPT-вложения не скачиваются автоматически. Запрос нельзя принять, если предыдущая задача не принята. Повтор с тем же ключом и payload возвращает прежний ответ; другой payload с тем же ключом отклоняется.

Исполнитель забирает `QUEUED` → `RUNNING`. Он использует `codex exec --sandbox workspace-write`, schema для результата и существующую локальную авторизацию Codex. У него отключена пользовательская конфигурация и дополнительные execpolicy rules, нет bypass sandbox; cwd закреплён на проекте. Файлы/тесты/критерии/отклонения возвращаются структурированно. Сырые события не выдаются ChatGPT и удаляются после обычного завершения.

Result сохраняется, успешное исполнение получает `AWAITING_REVIEW`. Architect через `task_result` читает отчёт и историю, через `read_file`/`git_diff` проверяет фактический код. `git_diff` показывает tracked-изменения; новые untracked-файлы нужно читать отдельно. Непройденные критерии дают `EXECUTION_FAILED`: его нельзя принять как успешный.

`review_task` требует точную revision:

- `ACCEPT` → `ACCEPTED`, затем разрешён следующий `submit_task`.
- `FIX` → следующая revision той же задачи и `QUEUED`, инструкции дополнены замечаниями.
- Старое ревью, повторное исполнение с прежним run ID и преждевременная приёмка отклоняются.

Одна задача исполняется в проекте одновременно. Для разных проектов работают отдельные очереди и исполнители; сбой одного не меняет состояние другого. Проверка критериев в result подтверждает полноту отчёта, но статус PASS остаётся утверждением Codex, которое Architect проверяет по коду и свидетельствам. Это не универсальный формальный доказатель корректности.

Для запуска ровно одной уже поставленной задачи локально:

```sh
.venv/bin/python -m bridge.cli execute-once OSKZ-IIKO-BACKOFFICE
```

## Добавление проекта

1. Скопируйте `config/project.example.json` в `config/projects/<name>.local.json`.
2. Заполните уникальные project ID, Architect thread и реальные пути. Укажите `deny_paths`, если в проекте есть чувствительные данные вне стандартных мест.
3. Запустите `init`: он проверит профиль и создаст `.ai-bridge`, не перезаписывая существующую архитектуру. Дублирующиеся/вложенные workspace и один Architect на два проекта запрещены.
4. Выполните `reboot`, затем отдельную OAuth-авторизацию нового проекта. При добавлении проекта прежние токены сохраняются, но не получают доступ к новой папке.
5. Поставьте небольшую задачу, проверьте результат и ревью, затем включите `auto_execute`.

Локальные `.local.json`, `.runtime`, Keychain и очереди не включайте в публичный репозиторий. При необходимости добавьте `.ai-bridge/` в локальный Git exclude проекта; установщик не изменяет его автоматически.

## Восстановление после перезапуска

```sh
.venv/bin/python -m bridge.cli status
.venv/bin/python -m bridge.cli doctor
.venv/bin/python -m bridge.cli reboot
```

`on` включает желаемое состояние и проверяет `/health` + отказ неавторизованного `/mcp` (401). `off` закрывает локальный сервис, сохраняя OAuth. `reboot` выполняет остановку/запуск и отказывается менять intentional Off. Занятый чужим процессом порт не приводит к убийству процесса: запуск не проходит проверку/блокировку.

SQLite transactions, fsync и блокировки сохраняют очередь. Если процесс упал между фиксацией состояния и экспортом JSON, повторный `init` восстановит `state.json` и остальные проекции из БД. История результатов/ревью прежних revisions сохраняется.

Если Codex был прерван, bridge не запускает задачу повторно автоматически. При исчезнувшем исполнителе статус становится `INTERRUPTED`. Если дочерний Codex ещё работает, восстановление сохраняет `RUNNING`; даже возможное повторное использование PID трактуется осторожно. Сначала проверьте живой процесс и рабочий diff, затем:

```sh
.venv/bin/python -m bridge.cli recover OSKZ-IIKO-BACKOFFICE
# Только после проверки исходников и отсутствия живого исполнителя:
.venv/bin/python -m bridge.cli retry OSKZ-IIKO-BACKOFFICE TASK-003
```

Ошибку исполнения можно исправить через Architect FIX или повторить локально после выяснения причины. ACCEPT не выдаёт разрешение на commit/push/install/deploy. Такие действия остаются отдельным поручением человеку/Codex.

Для отзыва OAuth:

```sh
.venv/bin/python -m bridge.cli rotate
```

`rotate` выключает bridge, удаляет grants и меняет пароль владельца в Keychain. После него требуются `on` и повторная OAuth-авторизация. Резервируйте queue.sqlite3 через SQLite backup API при работающем процессе; либо остановите bridge и сохраните всю `.ai-bridge`. Потеря Keychain мастер-ключа означает потерю старой OAuth-state: нужна новая авторизация, очередь задач при этом не теряется.

Удаление LaunchAgents:

```sh
.venv/bin/python -m bridge.cli off
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/org.codex.ai-bridge.server.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/org.codex.ai-bridge.executor.plist
rm ~/Library/LaunchAgents/org.codex.ai-bridge.server.plist ~/Library/LaunchAgents/org.codex.ai-bridge.executor.plist
```

## Безопасность и DevSpace

У upstream DevSpace есть write_file/edit_file/run_shell. Его уровни разрешений — политика, а не принудительная read-only граница. Поэтому он **не используется в открытом Architect-профиле**. Совместимый стандарт MCP сохранён через официальный Python SDK; оригинальный DevSpace/Windows workflow сохранён в исходных файлах как отдельный legacy-вариант. Нельзя запускать исходный installer/DevSpace endpoint и считать, что новый read-only профиль его ограничивает.

Публичные инструменты: `project_info`, `list_files`, `read_file`, `search_files`, `git_status`, `git_diff`, `submit_task`, `task_result`, `review_task`. Никаких generic write/shell/install/git commit/push tools. Результат добавляет только локальный Codex executor.

Чтение ограничено типами текстовых исходников/документов. Запрещены `.env*` (включая example), `.git`, credentials/secrets/password/token paths, `.ssh/.aws`, `.npmrc`, Keychain/runtime, private keys, storage, базы, логи, build/dependency-каталоги, symlinks, hardlinks и специальные файлы. Traversal и абсолютные пути отклоняются; файлы открываются через directory descriptors с O_NOFOLLOW. Поиск — literal, ограничен 100 результатами и 10 000 entries; файл ≤256 KB. Git принимает только фиксированные status/diff без shell, fsmonitor, external diff/textconv и пользовательских flags; чувствительные paths исключены.

Распространённые credential patterns маскируются в содержимом и result. Это дополнительная защита, не доказательство отсутствия произвольного секрета в разрешённом исходнике. Если секрет случайно записан в обычный документ, исключите этот документ через `deny_paths` и устраните утечку. Codex и bridge работают от одного macOS пользователя: Keychain защищает хранение, но не изолирует уже работающий локальный executor от прав этого пользователя. Для более строгой OS-изоляции нужен отдельный пользователь/VM.

## Проверки

```sh
.venv/bin/python -m pytest -q
```

Тесты проверяют project isolation, запрет secrets/traversal/symlink/hardlink/FIFO, подавление external Git diff, идемпотентность, crash recovery, FIX/ACCEPT, следующие задания, реальный HTTP MCP, OAuth/PKCE, replay кода, refresh/revoke и сохранение токенов после реконструкции сервера. Изолированная настоящая Codex-проверка описана отдельно в `VALIDATION.md`. Она не является product acceptance iiko.


## Secure MCP Tunnel на macOS


Локальный MCP можно подключить из ChatGPT через официальный `tunnel-client`, без публичного входящего адреса. Туннель должен быть связан с вашей Platform organization и рабочим пространством ChatGPT. Runtime API key хранится в подтверждённом игнорируемом `.env.local` как `OPENAI_API_KEY`, с правами `0600`; OAuth owner/encryption остаются в Keychain. Не помещайте ключ в YAML или аргументы процесса.

Локальная установка использует `.runtime/tunnel-client` и `.runtime/tunnel.yaml`; LaunchAgent `org.codex.ai-bridge.tunnel` запускает `.venv/bin/python -m bridge.tunnel`. Supervisor читает существующий `.runtime/desired-state.json`: Off останавливает туннель, On запускает его, перезапуск Mac сохраняет выбранное состояние. Ошибки и журналы находятся в `.runtime/tunnel-supervisor.log` и `.runtime/tunnel.log`; сырое HTTP-логирование выключено.

Для OAuth с локальным HTTP-сервером настройка tunnel-client разрешает HTTP только для выбранных локальных маршрутов (`harpoon.allow_plaintext_http: true`), отключает автоматическое включение private IP hosts (`harpoon.hosts_include_private: false`). Разрешённые OAuth-маршруты обнаруживаются из метаданных bridge. MCP сохраняет обязательную авторизацию. Сервер отдаёт одинаковые метаданные с завершающим `/` и без него, чтобы избежать запрещённого перенаправления в relay.

В ChatGPT выберите создание пользовательского сервера MCP, подключение Tunnel, OAuth и DCR. Базовые scopes: `bridge` и `project:<bridge_project_id>`. При переписывании Resource discovery туннелем укажите исходный ресурс локального сервера (`http://127.0.0.1:<port>/mcp`). После установки плагина отдельно завершите OAuth-вход; статус «Установлен» ещё не означает успешную авторизацию. Проверка `/readyz` также не заменяет вызов `project_info` из ChatGPT.

DCR по умолчанию регистрирует допустимые project scopes; это список доступных для запроса областей, а не выданный доступ. Фактический токен ограничивает выбор одного проекта после подтверждения владельца. Форма подтверждения защищена случайным одноразовым токеном, его серверным digest и HttpOnly SameSite=Strict cookie; Origin от посторонних сайтов отклоняется. Origin `null` встроенного браузера принимается только при успешной проверке токена формы и cookie, затем требуется пароль владельца. CSP разрешает возврат только на origin зарегистрированного callback.
