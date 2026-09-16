# investigate-feature Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Добавить в плагин `claude-atlassian` скилл `investigate-feature` — не-баговую половину развилки после `analyze-jira-ticket`: он заземляет постановку в коде и передаёт бриф в `superpowers:brainstorming`.

**Architecture:** Скилл — это два markdown-файла: `SKILL.md` с процессом и контрактом и `references/scout-prompts.md` с шаблонами промптов для субагентов. Исполняемого кода нет: поведение задаётся текстом, который читает модель. Структура повторяет соседний `investigate-bug` (гейты → read-only контракт → разведка скаутами → синтез → проверочный раунд → отчёт → исходы), но содержание другое: вместо поиска причины — постановка, точки вставки, прецеденты и развилки. Общих файлов между двумя скиллами нет, каждый самодостаточен.

**Tech Stack:** Markdown (формат скиллов Claude Code), Python 3 из стандартной библиотеки — только для проверочных сниппетов в этом плане, git. Новых зависимостей и новых артефактов сборки работа не добавляет.

**Spec:** `docs/superpowers/specs/2026-09-16-investigate-feature-design.md`

## Global Constraints

- **Язык артефактов:** `SKILL.md` и `scout-prompts.md` пишутся по-английски, как остальные скиллы плагина. Строка `Write the report in the user's language.` обязана присутствовать в `SKILL.md` — отчёт выдаётся на языке пользователя.
- **Heredoc только с закавыченным делимитером** (`<<'MD'`, `<<'PY'`). При незакавыченном `${CLAUDE_PLUGIN_ROOT}` и `${CLAUDE_SKILL_DIR}` схлопнутся в пустую строку, и скилл молча потеряет пути. После записи файла проверять, что литералы `${CLAUDE_...}` остались на месте.
- **`tests/` не трогать:** там тесты загрузчика вложений, к этой работе отношения не имеют. Новых тестовых файлов работа не заводит — автотестов на текст скиллов в репозитории нет по решению спека.
- **Версию плагина не поднимать:** изменения ложатся в `[Unreleased]` в `CHANGELOG.md`; bump — решение момента релиза.
- **Новых каталогов, кроме `skills/investigate-feature/`, не создавать.**
- **Скилл read-only по контракту:** в его тексте не должно появиться ни одной инструкции, разрешающей править файлы, ставить зависимости или писать в Jira.
- **Ветка:** `feature/investigate-feature`. Каталог `docs/superpowers/**` удаляется из дерева (`git rm`) перед созданием PR и остаётся в истории ветки.
- **Коммит после каждой задачи**, сообщение — по-английски, как единственный коммит в истории репозитория.

---

### Task 1: Шаблоны скаут-промптов

✅ Done — see commit(s): `7758a1a`, `aa16462`, `de57b89` (+ правки финального ревью в `a45f691`)

---

### Task 2: SKILL.md — контракт и гейты

✅ Done — see commit(s): `cfc7e03`, `7513e04`, `4ef3a68`, `8a4b233` (+ правки финального ревью в `a45f691`)

---

### Task 3: SKILL.md — разведка, синтез, проверка на прочность

✅ Done — see commit(s): `8326f0b` (+ амендменты в `c055f71`, правки финального ревью в `a45f691`)

---

### Task 4: SKILL.md — отчёт, исходы, передача

✅ Done — see commit(s): `c055f71`, `e072c89`, `96c705a` (+ правки финального ревью в `a45f691`)

---

### Task 5: Плагин узнаёт о новом скилле

✅ Done — see commit(s): `9538430`, `c0b5798` (+ правка CHANGELOG в `a45f691`)

---

### Task 6: Живой прогон

**Эту задачу выполняет человек.** Субагент её сделать не может: нужен доступ к реальному Jira через MCP и решение о том, хорошо ли скилл себя ведёт. Задача ничего не коммитит, кроме правок текста скилла, если прогон их потребует.

**Files:**
- Modify (только если прогон выявит дефекты): `skills/investigate-feature/SKILL.md`, `skills/investigate-feature/references/scout-prompts.md`

**Interfaces:**
- Consumes: готовый скилл из задач 1-5.
- Produces: подтверждение, что скилл работает, либо список правок.

- [ ] **Step 1: Прогон на реальном не-баговом тикете**

```
/claude-atlassian:analyze-jira-ticket <KEY>
/claude-atlassian:investigate-feature <KEY>
```

Проверить по ходу:
- скауты ушли одним сообщением, а не по очереди;
- в отчёте все девять секций и он на русском;
- у каждого критерия приёмки проставлен источник, выведенные помечены;
- в «Where it lands» есть координаты, а не только имена файлов;
- исход выбран один и соответствует таблице;
- при согласии на `brainstorming` он не начинает разведку проекта заново.

- [ ] **Step 2: Проверка отказов**

| Что запустить | Ожидаемое поведение |
|---------------|---------------------|
| `/claude-atlassian:investigate-feature` на баговом тикете | Останов на втором гейте, предложение `investigate-bug` |
| `/claude-atlassian:investigate-feature PROJ-123` в свежем сеансе без саммари | Останов на первом гейте, просьба сначала запустить `analyze-jira-ticket` |
| То же вне git-репозитория (например, из `/tmp`) | Останов на третьем гейте, вопрос, где лежит код |

- [ ] **Step 3: Записать правки, если они нужны**

Дефекты правятся прямо в тексте скилла, одним коммитом на дефект:

```bash
cd /opt/github/zinin/claude-atlassian && git add skills/investigate-feature && git commit -m "Fix investigate-feature: <что именно поплыло в прогоне>"
```

- [ ] **Step 4: Убрать документы superpowers перед PR**

```bash
cd /opt/github/zinin/claude-atlassian && git rm -r --cached docs/superpowers && rm -rf docs/superpowers && git commit -m "Remove design and plan documents from the tree"
```

Документы остаются доступны в истории ветки: `git show HEAD~1:docs/superpowers/plans/2026-09-16-investigate-feature.md`.
