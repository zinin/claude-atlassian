## TASK

Continue executing the implementation plan for the `investigate-feature` skill (Claude Code plugin `claude-atlassian`, branch `feature/investigate-feature`).

## CRITICAL: DO NOT START WORKING

**STOP. READ THIS CAREFULLY.**

After loading all context below, you MUST:
1. Read the documents and understand the context
2. Report what you understood (brief summary)
3. **WAIT for explicit user instructions** before taking ANY action

**DO NOT:**
- Start implementing tasks
- Make any code changes
- Run any commands (except reading documents)
- Assume what task to work on next

**The user will tell you exactly what to do.** Until then, only read and summarize.

## DOCUMENTS

- Design: `docs/superpowers/specs/2026-09-16-investigate-feature-design.md`
- Plan: `docs/superpowers/plans/2026-09-16-investigate-feature.md` (задачи 1-5 уже свёрнуты до ссылок на коммиты)
- SDD-ледгер (git-ignored, главный источник состояния): `.superpowers/sdd/2026-09-16-investigate-feature/progress.md`

Read the design and the plan to understand the full picture; read the ledger to know exactly where execution stopped.

## PROGRESS

**Completed tasks:**
- [x] Task 1: шаблоны скаут-промптов `skills/investigate-feature/references/scout-prompts.md`
- [x] Task 2: `SKILL.md` — frontmatter, гейты, read-only контракт, рационализации, red flags
- [x] Task 3: `SKILL.md` — Recon, Synthesis, Strength check
- [x] Task 4: `SKILL.md` — Report, Outcomes (пять исходов, включая `Blocked`), передача в brainstorming
- [x] Task 5: маршрут в `investigate-bug`, README, CHANGELOG, манифест

Каждая задача прошла ревью задачи (spec + quality) и точечное ревью после каждого раунда правок.

**In progress — final whole-branch review phase:**
- [x] Финальное ревью ветки: With fixes (0 Critical, 5 Important, 6 Minor)
- [x] Фикс-волна применена одним коммитом `a45f691` (12 правок), проверки брифов зелёные
- [ ] **Точечное ревью фикс-волны — НЕ запущено.** Это следующий шаг.
- [ ] Разбор остатков после него (второй фикс-волны по протоколу SDD нет — остатки паркуются с решением или решаются контролёром)
- [ ] Finish SDD: собрать все строки `Ruling` из ледгера (сейчас 23) в финальное сообщение пользователю; удалить workspace `.superpowers/sdd/2026-09-16-investigate-feature/`
- [ ] `superpowers:finishing-a-development-branch` — перед ним предложить внешнее код-ревью

**Remaining tasks:**
- [ ] Task 6: живой прогон на реальном тикете — выполняет ПОЛЬЗОВАТЕЛЬ (нужен Jira через MCP), субагенту не отдавать (Ruling 2). Шаг 4 этой задачи (`git rm docs/superpowers`) — только перед PR (Ruling 3).

## SESSION CONTEXT

**Точечное ревью фикс-волны — всё готово:**
- Пакет диффа: `.superpowers/sdd/2026-09-16-investigate-feature/review-c0b5798..a45f691.diff` (FIX_BASE=`c0b5798`, HEAD=`a45f691`)
- Список находок с точными текстами: `.superpowers/sdd/2026-09-16-investigate-feature/final-fix-brief.md`
- Отчёт исполнителя: `.superpowers/sdd/2026-09-16-investigate-feature/final-fix-report.md`
- Шаблон: `re-review-prompt.md` из скилла `superpowers:subagent-driven-development`
- **Названный риск:** правка M1b — строка тегов источника (`ticket | discussion | mockup | attachment | wiki | inferred`) в шаблоне отчёта перенесена ВНУТРИ `~~~`-блока. Шаблон модель копирует буквально, перенос может исказить форму строки критерия; вероятно, строку надо вернуть в одну (101 колонка). Решение за ревьюером.

**Режим исполнения (из `/claude-mesh:do-plan`):** dispatch model = `opus` для всех субагентов; предыдущий порог STOP был 400k.

**Особенности харнесса, выученные на этом прогоне:**
- Второе сообщение исполнителю в рамках одного раунда (SendMessage, пока он работает) несколько раз не доезжало — он заканчивал ход раньше. **Все правки раунда отправлять одним сообщением**, результат проверять grep'ом по файлу, а не по отчёту.
- Отчёты ревьюеров обрезаются каналом доставки. В промпт ревьюера писать лимит строк (≤50) и ставить вердикт ПЕРВЫМ.
- Сообщения субагентов часто приходят повторно (idle-нотификации) — это дубли, действовать по ним не нужно.

**Принятые решения, которые стоит знать (полный список — строки `Ruling` в ледгере):**
- Спек — связывающая инстанция, но часть его текста сознательно изменена по итогам ревью: пять исходов вместо четырёх (фатальный блокер выделен в `Blocked`); description без `: ` ради строгого YAML; гейт `investigate-feature` сделан точной инверсией гейта `investigate-bug` («Nothing observably misbehaves …»), иначе дефект без симптома ходил между скиллами по кругу.
- `investigate-bug` в этой ветке больше НЕ трогать (совет финального ревью). Отложено в отдельный PR сразу для обоих скиллов: разделители вокруг `{EVIDENCE}`, место правила про вложения в гейте 1, `mkdir` до подтверждения, граница прогона и git-запрет в собственном контракте и шаблонах `investigate-bug`.
- Встроенные в план копии файлов синхронизировались с артефактами до Task 4 включительно; задачи 1-5 теперь свёрнуты, так что это не важно.

**Проверки:**
- Тесты репозитория (только загрузчик вложений, ветка его не трогает): `PYTHONPATH=scripts python3 -m unittest discover -s tests -t . -q` → 172 теста, OK. Без `PYTHONPATH=scripts` запускается 6 тестов с ошибками импорта.
- Проверочные сниппеты задач лежат в брифах `.superpowers/sdd/2026-09-16-investigate-feature/task-{1..5}-brief.md` (Step 1; у task-4 ещё Step 4) — их можно гонять повторно как регресс.

**До PR (за пользователем или по его явной команде):**
- `git rm -r docs/superpowers` — требование глобального CLAUDE.md пользователя.
- При релизе сверить description плагина в каталоге маркетплейса `zinin/claude-plugins` — там может остаться «только баги».
- Версию плагина не поднимать: изменения в `[Unreleased]`.

**Правила пользователя (глобальный CLAUDE.md):** локальную память `~/.claude/projects/.../memory/` не использовать; дизайн и план живут на feature-ветке; перед PR `git rm` всё из `docs/superpowers/`.

## PLAN QUALITY WARNING

The plan was written for a large task and may contain:
- Errors or inaccuracies in implementation details
- Oversights about edge cases or dependencies
- Assumptions that don't match the actual codebase
- Missing steps or incomplete instructions

**If you notice any issues during implementation:**
1. STOP before proceeding with the problematic step
2. Clearly describe the problem you found
3. Explain why the plan doesn't work or seems incorrect
4. Ask the user how to proceed

Do NOT silently work around plan issues or make significant deviations without user approval.

## INSTRUCTIONS

1. Read the documents listed above
2. Understand current progress and session context
3. Provide a brief summary of what you understood
4. **STOP and WAIT** — do NOT proceed with any implementation
5. Ask: "What would you like me to work on?"
