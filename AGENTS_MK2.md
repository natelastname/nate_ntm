# AGENTS_MK2: Spec‑Kit Doctrine

This file defines how AI coding agents (including OpenHands) should
interpret and execute **spec‑kit slash commands** in this repository.
Read this **in addition to** `AGENTS.md` and the files under
`.specify/`.

- `AGENTS.md` → RU- and migration‑specific guidance
- `AGENTS_MK2.md` → spec‑kit doctrine and slash‑command behavior
- `.specify/` → canonical spec‑kit command templates and project constitution

**Notation in this repo:** Spec-kit commands are canonically named `/speckit.<name>`,
but humans and agents will usually invoke them as `./speckit.<name>` in this
repository to avoid host slash-command parsing. Treat `/speckit.<name>` and
`./speckit.<name>` as equivalent ways to invoke the same command.

## 1. Mental model: Spec‑driven development in this repo

Spec‑kit turns feature work into an explicit pipeline:

1. **Constitution** – Project‑wide principles and non‑negotiable rules
   - `/speckit.constitution`
2. **Specification** – Natural‑language feature spec
   - `/speckit.specify` (create spec + feature directory)
   - `/speckit.clarify` (tighten ambiguous specs)
3. **Planning** – Technical design and architecture
   - `/speckit.plan`
4. **Tasks and Checklists** – Execution plan and “unit tests for English”
   - `/speckit.tasks` (tasks.md)
   - `/speckit.checklist` (requirements‑quality checklists)
5. **Analysis** – Cross‑artifact consistency / coverage
   - `/speckit.analyze`
6. **Implementation** – Execute tasks and update the codebase
   - `/speckit.implement`
7. **Operationalization** – Turn tasks into issues
   - `/speckit.taskstoissues`

**Default expectation:** For non‑trivial new work, follow this
constitution → spec → clarify → plan → tasks → analyze → implement
pipeline rather than jumping straight to implementation.

If the user clearly opts out of spec‑kit (e.g., “just fix this small
bug, don’t use spec‑kit”), follow the normal RU/OpenHands guidance
instead.

---

## 2. Recognizing and interpreting spec‑kit slash commands

### 2.1 What counts as a spec‑kit slash command

Treat any of the following as a spec‑kit command invocation:

- A message that **starts with** `/speckit.<name>` or `./speckit.<name>`
- A message that contains `/speckit.<name>` or `./speckit.<name>` on its own line
- A message where the user explicitly says they are “running” a
  `speckit.<name>` command (even if it appears in backticks, with or without the leading `./`)

Where `<name>` is one of:

- `constitution`
- `specify`
- `clarify`
- `plan`
- `tasks`
- `checklist`
- `analyze`
- `implement`
- `taskstoissues`

### 2.2 Mapping commands to templates

For each known command name `<name>`:

- The canonical behavior is defined in:
  - `.specify/speckit.<name>.md`
- That file is a **template + procedure** for the agent to follow.
- The `description` and sections (`User Input`, `Pre‑Execution Checks`,
  `Outline`, `Phases`, `Execution Steps`, etc.) are **authoritative**
  for that command’s behavior.

**Rule:** When a user invokes `/speckit.<name>` or `./speckit.<name>`, you
are now executing **that command’s playbook**. Your job is to follow the
corresponding `speckit.<name>.md` as your primary instruction source,
subject to repository security constraints.

### 2.3 Interpreting `$ARGUMENTS` / user input

Each template contains a `User Input` section with

```text
$ARGUMENTS
```

For OpenHands / chat‑based agents, interpret this as:

- The **text after the slash command** in the user’s message is
  `$ARGUMENTS`.
  - Example: `/speckit.specify Implement a new RU status dashboard` →
    `$ARGUMENTS` = `Implement a new RU status dashboard`.
- If the command appears alone (no trailing text), treat `$ARGUMENTS`
  as empty and:
  - Check whether the template allows empty input.
  - If not, respond with a clear error that the feature description or
    context is missing and request only the minimum additional input
    required.
- **Do not** ask the user to restate arguments that are already visible
  in the triggering message.

Whenever a template says “You **MUST** consider the user input”, you
should:

- Quote or summarize `$ARGUMENTS` at the start of your reasoning.
- Make sure your decisions and artifacts can be traced back to that
  input.

### 2.4 When multiple commands appear

If a single message mentions multiple spec-kit commands (any mix of
`/speckit.*` or `./speckit.*`):

- Default assumption: the **first** command is the one you should
  execute now; later commands are likely suggested follow‑ups.
- If the user explicitly says “run A then B”, complete A first, then
  proceed to B, re‑reading the relevant template before starting B.
- Do **not** interleave execution steps from different commands.

If the user invokes an unknown spec-kit command name:

- Check whether a matching `.specify/speckit.<name>.md` exists.
- If not found, treat it as a normal string and explain that only the
  documented commands above are supported.

---

## 3. Shared runtime patterns across spec‑kit commands

### 3.1 `.specify/` layout and core files

Key directories and files:

- `.specify/speckit.*.md` – Command templates (you are reading and
  obeying these).
- `.specify/memory/constitution.md` – Live project constitution;
  derived from a template and updated via `/speckit.constitution`.
- `.specify/templates/` – Source templates for specs, plans, tasks,
  checklists, and command behavior.
- `.specify/scripts/bash/*.sh` – CLI helpers that return JSON and file
  paths (used by several commands).
- `.specify/feature.json` – JSON describing the **current feature
  directory**:

  ```json
  { "feature_directory": "specs/003-feature-name" }
  ```

  This is written by `/speckit.specify` and read by downstream steps.

Always treat these as **project‑controlled infrastructure**. Do not
rename or relocate them unless you are explicitly editing the spec‑kit
integration itself.

### 3.2 Extension hooks (`.specify/extensions.yml`)

Many templates contain “Check for extension hooks” sections. The
convention is:

- Hooks live in `.specify/extensions.yml` under keys like
  `hooks.before_specify`, `hooks.after_plan`, etc.
- Each hook entry may include:
  - `extension`: logical extension name
  - `command`: a **slash command name** (no leading `/`)
  - `description`: human summary
  - `prompt`: text to show as a follow‑up instruction
  - `optional`: boolean (default `false` if omitted)
  - `enabled`: boolean (default `true` if omitted)
  - `condition`: arbitrary expression – **do not evaluate** in the
    agent; condition handling is for the host/HookExecutor.

When a template tells you to “Check for extension hooks”:

1. **Read** `.specify/extensions.yml` if it exists.
2. Filter hooks:
   - Ignore entries where `enabled: false`.
   - Treat missing `enabled` as enabled.
   - **Do not** try to interpret `condition`; if present and
     non‑empty, skip the hook (per template rules).
3. For each executable hook, **emit exactly the Markdown blocks** shown
   in the template with the appropriate values:

   - **Optional hooks**:

     ```markdown
     ## Extension Hooks

     **Optional Pre‑Hook**: {extension}
     Command: `/{command}`
     Description: {description}

     Prompt: {prompt}
     To execute: `/{command}`
     ```

   - **Mandatory / automatic hooks**:

     ```markdown
     ## Extension Hooks

     **Automatic Pre‑Hook**: {extension}
     Executing: `/{command}`
     EXECUTE_COMMAND: {command}

     Wait for the result of the hook command before proceeding to the Outline.
     ```

4. If no hooks are configured or the YAML is invalid, **continue
   silently** with the rest of the command’s flow.

#### 3.2.1 `EXECUTE_COMMAND:` markers

Some templates (core and extension commands) show example blocks that
contain a line like:

```text
EXECUTE_COMMAND: <command>
```

In this repository, interpret that as a **directive to actually run
`<command>` as its own spec‑kit command**, not as a hint for some
separate orchestrator.

When you follow a template that includes such a block:

- Treat `<command>` as the name of another spec‑kit command (for
  example, `speckit.git.feature` or `speckit.git.commit`).
- Execute that command as a **separate, delegated run**:
  - In environments with subagent support (for example, OpenHands using
    delegated tasks), prefer to start a new run bound to the matching
    markdown template (such as
    `.specify/extensions/git/commands/speckit.git.feature.md`), let it
    complete (including any shell scripts it calls), then resume the
    original `/speckit.*` command with the updated context.
  - In simpler environments without subagents, treat
    `EXECUTE_COMMAND: <command>` as a strong instruction to run the
    referenced `./speckit.*` command sequentially, but still as a
    logically separate phase from the current command.
- When you describe what happened in your reply, you may still include
  the `EXECUTE_COMMAND: <command>` line (usually inside an “Extension
  Hooks” block) so humans and tools can see which delegated command was
  conceptually executed. If you include it, keep the wording and
  formatting exactly as shown in the template.

Key points:

- Do **not** wait for some external “outer orchestrator” to react to
  `EXECUTE_COMMAND:`; in this repo, you are expected to carry out the
  referenced command yourself via delegation or sequential execution.
- Keep flows clearly ordered: finish the delegated command first, then
  continue with the rest of the current `/speckit.*` command.


---

### 3.3 Feature-specific docs: nate_ntm runtime and nate-oha ACP

For work on the nate_ntm Swarm Runtime Orchestrator and its ACP adapters:

- Feature 001 (`specs/001-swarm-runtime-orchestrator/`) defines the core
  runtime, configuration model, and MVP quickstart.
- Feature 002 (`specs/002-nate-oha-acp-adapter/`) defines the nate-oha
  production ACP adapter (`NateOhaAcpClient`) and its process-launch
  contract.

When you are editing runtime/adapter code or docs:

- Treat `NateOhaAcpClient` as the canonical **production** ACP adapter for
  REAL mode in this repository.
- Consider `OpenHandsAcpClient` a legacy/compatibility path only.
- Use the quickstarts under
  `specs/001-swarm-runtime-orchestrator/quickstart.md` and
  `specs/002-nate-oha-acp-adapter/quickstart.md` as the primary sources for
  end-to-end validation flows.
- For deeper details, consult each feature directory's `spec.md`, `plan.md`,
  and `tasks.md` files.

### 3.4 `.specify/scripts/bash/*.sh` helpers

Several commands rely on shell helpers that emit JSON, notably:

- `check-prerequisites.sh` – determines the current feature directory
  and artifacts.
- `setup-plan.sh` – resolves paths for planning artifacts
  (spec, plan template, etc.).
- `setup-tasks.sh` – resolves paths and context for task generation.

General rules:

- **Always run these from the repo root** as instructed in the
  template.
- Use the exact flags shown (e.g., `--json`, `--paths-only`,
  `--require-tasks`, `--include-tasks`).
- Treat these scripts as the **single source of truth** for the current
  feature’s file layout; do not guess or hard‑code paths they already
  provide.
- If a script fails or JSON parsing fails, follow the template’s
  fallback (usually: abort the command and tell the user what they need
  to run first, often `./speckit.specify`).

### 3.5 Constitution precedence

`.specify/memory/constitution.md` defines **project‑wide non‑negotiable
rules**. Templates (especially `speckit.plan`, `speckit.analyze`, and
`speckit.implement`) treat it as authoritative:

- If there is a conflict between a feature spec, a plan, or tasks and a
  **MUST** principle in the constitution, that is a **CRITICAL
  problem**.
- You may propose changes to specs/plans/tasks to restore compliance.
- You **must not** silently override or “reinterpret” constitutional
  principles during `/speckit.analyze` or `/speckit.implement`.
- Changes to the constitution must be made explicitly via
  `/speckit.constitution`.

### 3.5 Read‑only vs mutating commands

- **Read‑only** (no file writes):
  - `/speckit.analyze` (explicitly non‑destructive)
- **Mutating** (write or create artifacts):
  - `/speckit.constitution`
  - `/speckit.specify`
  - `/speckit.clarify`
  - `/speckit.plan`
  - `/speckit.tasks`
  - `/speckit.checklist`
  - `/speckit.implement`
  - `/speckit.taskstoissues` (writes to GitHub via MCP / API)

For mutating commands, follow repository safety and testing guidelines
from `AGENTS.md` and `PYTHON_MIGRATE.md` in addition to spec‑kit
instructions.

---

## 4. Slash‑command reference

The sections below summarize **how to execute** each command in this
repo. For full detail, re‑read the corresponding `.specify/speckit.*.md`
file before starting.

### 4.1 `/speckit.constitution`

Template: `.specify/speckit.constitution.md`

Purpose:

- Maintain `.specify/memory/constitution.md` as a concrete, versioned
  constitution derived from its template.
- Propagate changes into dependent templates (spec, plan, tasks, and
  command templates) and emit a “Sync Impact Report”.

Key points:

- Always treat `.specify/memory/constitution.md` as the document you
  are updating; **do not** create a second constitution file.
- Use placeholders `[ALL_CAPS_IDENTIFIER]` in the template as cues to
  fill in real values from:
  - User input
  - Existing repo docs (README, previous constitution versions, etc.)
- Apply semantic versioning to `CONSTITUTION_VERSION`:
  - MAJOR: backwards‑incompatible changes to principles/governance.
  - MINOR: add or materially expand guidance.
  - PATCH: clarifications and non‑semantic edits.
- After updating the constitution, check the relevant templates listed
  in the `Consistency propagation checklist` section and update them as
  needed so they remain aligned.
- Pre‑ and post‑execution hooks (`hooks.before_constitution`,
  `hooks.after_constitution`) must be processed as described in
  §3.2.

When to use:

- When the project wants to change or clarify high‑level principles,
  governance, or quality bars that should apply to all features.

### 4.2 `/speckit.specify`

Template: `.specify/speckit.specify.md`

Purpose:

- Turn a natural‑language feature description into a concrete spec
  file, feature directory, and an initial spec‑quality checklist.

High‑level flow:

1. **Interpret the feature description** from `$ARGUMENTS` as the
   canonical problem statement.
2. Generate a **short feature name** (2–4 words, slug‑style) from the
   description.
3. Optionally integrate with branching hooks via `before_specify`:
   - Hooks may create or switch a git branch and emit JSON with
     `BRANCH_NAME` and `FEATURE_NUM`.
   - Respect but do not depend on branch naming for feature directory
     resolution.
4. **Resolve feature directory (`SPECIFY_FEATURE_DIRECTORY`)**:
   - If user provides `SPECIFY_FEATURE_DIRECTORY`, use it directly.
   - Otherwise, auto‑generate under `specs/` using branch numbering
     rules from `.specify/init-options.json` (`sequential` vs
     `timestamp`).
5. **Create directory and spec file**:
   - `mkdir -p SPECIFY_FEATURE_DIRECTORY`
   - Copy `.specify/templates/spec-template.md` →
     `SPECIFY_FEATURE_DIRECTORY/spec.md` (this becomes `SPEC_FILE`).
   - Persist the resolved directory in `.specify/feature.json`.
6. **Fill the spec** by following the template’s execution flow:
   - Extract actors, actions, data, and constraints.
   - Use `[NEEDS CLARIFICATION: ...]` markers sparingly (max 3) for
     critical unknowns.
   - Write complete sections (user scenarios, functional requirements,
     success criteria, entities, etc.).
7. **Generate a Specification Quality Checklist** at
   `SPECIFY_FEATURE_DIRECTORY/checklists/requirements.md` using the
   checklist template described in the command file.
8. Validate spec quality and iterate up to 3 times if needed.

Critical rules:

- Only create **one feature** per `/speckit.specify` invocation.
- The spec directory name and git branch name are **independent**.
- The spec directory and file are **always** created by
  `/speckit.specify`, not by hooks.

When to use:

- Any time the user wants to start a new feature using spec‑driven
  development.

### 4.3 `/speckit.clarify`

Template: `.specify/speckit.clarify.md`

Purpose:

- Identify underspecified areas in the current spec and resolve them via
  up to 5 high‑impact clarification questions, updating `spec.md` in
  place.

High‑level flow:

1. Run `check-prerequisites.sh --json --paths-only` to obtain:
   - `FEATURE_DIR`
   - `FEATURE_SPEC` (path to current spec)
2. Load `FEATURE_SPEC` and perform a structured ambiguity/coverage scan
   across categories such as functional scope, data model, UX flows,
   non‑functional requirements, integration, edge cases, constraints,
   terminology, and completion signals.
3. Build an internal queue of **at most 5** candidate questions that:
   - Are materially important.
   - Are answerable via multiple choice or constrained short answer.
4. Ask **one question at a time**, following the multiple‑choice or
   short‑answer patterns defined in the template.
5. After each accepted answer:
   - Ensure a `## Clarifications` section exists in the spec with a
     `### Session YYYY-MM-DD` heading for today.
   - Append a bullet `- Q: ... → A: ...` there.
   - Update the most relevant spec section (requirements, data model,
     success criteria, edge cases, terminology) to encode the answer.
   - Save `FEATURE_SPEC` after each integration.
6. At the end, report:
   - Number of questions asked/answered.
   - Path to updated spec.
   - Sections touched.

Critical rules:

- Max 5 questions total.
- Clarify only where it meaningfully reduces rework or risk.
- Maintain spec structure and avoid contradictory or duplicate
  statements.
- This command is expected to run **before** `/speckit.plan`.

When to use:

- After `/speckit.specify` but before planning, when the spec is likely
  incomplete or ambiguous.

### 4.4 `/speckit.plan`

Template: `.specify/speckit.plan.md`

Purpose:

- Produce a structured implementation plan (`plan.md` and related
  design artifacts) based on the spec and constitution.

High‑level flow:

1. Run `.specify/scripts/bash/setup-plan.sh --json` to obtain JSON with
   at least:
   - `FEATURE_SPEC`
   - `IMPL_PLAN` (path where plan file lives)
   - `SPECS_DIR`
   - `BRANCH`
2. Load:
   - `FEATURE_SPEC` (spec.md)
   - `.specify/memory/constitution.md`
   - The plan template already copied to `IMPL_PLAN`.
3. Fill out the plan template, including:
   - Technical context and unknowns (`NEEDS CLARIFICATION` where
     appropriate).
   - Constitution‑driven checks and gates.
   - Phase‑structured design sections (research, data model, contracts,
     quickstart, etc.).
4. Phase‑based artifact generation:
   - **Phase 0**: `research.md` resolving unknowns.
   - **Phase 1**: `data-model.md`, `contracts/` (if applicable),
     `quickstart.md`.
   - Update AI agent context by editing `AGENTS.md` between
     `<!-- SPECKIT START -->` and `<!-- SPECKIT END -->` to point to
     the current plan file.
5. Re‑evaluate constitution checks after design.
6. Stop after planning; report:
   - Branch
   - `IMPL_PLAN` path
   - Generated artifacts
   - Any gating issues.

When to use:

- After spec + clarification are done and before task generation.

### 4.5 `/speckit.tasks`

Template: `.specify/speckit.tasks.md`

Purpose:

- Generate an actionable `tasks.md` that breaks the plan into ordered,
  parallel‑friendly tasks organized by user story.

High‑level flow:

1. Run `.specify/scripts/bash/setup-tasks.sh --json` to obtain:
   - `FEATURE_DIR`
   - `TASKS_TEMPLATE` (absolute path)
   - `AVAILABLE_DOCS` under `FEATURE_DIR`.
2. Load design artifacts from `FEATURE_DIR`:
   - Required: `plan.md`, `spec.md`.
   - Optional: `data-model.md`, `contracts/`, `research.md`,
     `quickstart.md`.
3. Generate tasks organized by user story and phase, referencing actual
   files and components.
4. Write `tasks.md` using the template structure from `TASKS_TEMPLATE`,
   or `.specify/templates/tasks-template.md` as a fallback.
5. Each task **must** follow the strict checklist format:

   ```text
   - [ ] T001 [P] [US1] Description with file path
   ```

   where:

   - `T###` is a sequential task ID.
   - `[P]` is present only for safe parallelizable tasks.
   - `[USn]` labels user‑story phases only.
   - Description includes a concrete file path.

6. Report summary metrics: total tasks, tasks per story, parallel
   opportunities, MVP scope, and format validation.

When to use:

- After `/speckit.plan`, when design artifacts exist and we are ready to
  derive concrete implementation tasks.

### 4.6 `/speckit.checklist`

Template: `.specify/speckit.checklist.md`

Purpose:

- Generate **requirements‑quality** checklists (“unit tests for
  English”) for the current feature. These test the spec/plan
  **documents**, not the implementation.

Key doctrine:

- Checklists **do not** verify runtime behavior (“click button”, “API
  returns 200”).
- They validate whether the **requirements are well‑written**:
  completeness, clarity, consistency, measurability, coverage, edge
  cases, non‑functional aspects, dependencies, gaps, ambiguities, etc.

High‑level flow:

1. Run `check-prerequisites.sh --json` to obtain `FEATURE_DIR` and
   `AVAILABLE_DOCS`.
2. Optionally ask up to 3 focused clarification questions about the
   checklist’s domain, depth, audience, and scope.
3. Load `spec.md` (and optionally `plan.md`, `tasks.md`) using
   progressive disclosure, focusing only on parts relevant to the
   checklist theme.
4. Create or append to a checklist file in `FEATURE_DIR/checklists/`:
   - Short, descriptive filename per domain (e.g., `ux.md`,
     `security.md`).
   - Item IDs `CHK###` increment globally per file.
5. Generate items that:
   - Ask questions about the **requirements** (“Are X defined?”), not
     behavior.
   - Are grouped by requirement quality dimensions (completeness,
     clarity, consistency, acceptance‑criteria quality, scenario
     coverage, edge case coverage, NFRs, dependencies, ambiguities).
   - Include traceability markers (`[Spec §X.Y]`, `[Gap]`,
     `[Ambiguity]`, etc.).
6. Report path, item count, and summary of focus areas.

When to use:

- Any time you want a focused checklist to test whether the
  specification itself is ready for implementation or review in a
  particular domain (UX, security, API, etc.).

### 4.7 `/speckit.analyze`

Template: `.specify/speckit.analyze.md`

Purpose:

- Perform a **read‑only** consistency and coverage analysis across
  `spec.md`, `plan.md`, `tasks.md`, and the constitution.

High‑level flow:

1. Run `check-prerequisites.sh --json --require-tasks --include-tasks`
   to get `FEATURE_DIR` and `AVAILABLE_DOCS`; derive absolute paths for
   `spec.md`, `plan.md`, `tasks.md`.
2. Load only necessary sections from each artifact and from the
   constitution.
3. Build internal models of:
   - Requirements and success criteria (keys like `FR-###`, `SC-###`).
   - User stories and actions.
   - Task coverage mapping.
   - Constitution rules.
4. Run detection passes for duplication, ambiguity, underspecification,
   constitution misalignment, coverage gaps, and inconsistencies.
5. Assign severities (CRITICAL/HIGH/MEDIUM/LOW).
6. Output a **Markdown report** (no file writes) with:
   - A findings table (ID, category, severity, locations, summary,
     recommendation).
   - Coverage summary table (requirements ↔ tasks).
   - Constitution alignment issues.
   - Unmapped tasks.
   - Metrics (counts and coverage %).
7. Provide a concise “Next Actions” block and optionally offer a
   remediation proposal (but do **not** apply edits here).

When to use:

- After `tasks.md` exists, before or during `/speckit.implement`, to
  catch design/spec/task inconsistencies early.

### 4.8 `/speckit.implement`

Template: `.specify/speckit.implement.md`

Purpose:

- Execute the plan encoded in `tasks.md` in a structured,
  phase‑by‑phase manner, coordinating code changes, tests, and
  supporting artifacts.

High‑level flow:

1. Run `check-prerequisites.sh --json --require-tasks --include-tasks`
   to get `FEATURE_DIR` and supporting docs list.
2. **Check checklist status** if `FEATURE_DIR/checklists/` exists:
   - Scan all checklist files and build a status table.
   - If any checklist has incomplete items, present the table and ask
     whether to proceed anyway. Respect the user’s decision.
3. Load implementation context (tasks, plan, optional data model,
   contracts, research, constitution, quickstart).
4. Verify and, if needed, create/augment ignore files (`.gitignore`,
   `.dockerignore`, `.eslintignore`, etc.) based on detected stack and
   tools, following patterns in the template.
5. Parse `tasks.md` phases, dependencies, and markers.
6. Execute tasks:
   - Phase‑by‑phase; complete each phase before moving on.
   - Respect sequential vs parallel execution.
   - Follow TDD ordering where tests are present.
   - Coordinate file access to avoid concurrent edits to the same file.
7. Progress tracking and error handling:
   - Report progress per task.
   - Halt on critical sequential failures; for parallel tasks, continue
     successful ones but report failures.
   - Suggest next steps when blocked.
8. **Always mark completed tasks as `[X]` in `tasks.md`**, preserving
   formatting.
9. On completion, validate:
   - All required tasks for the chosen scope are complete.
   - Implementation matches the original spec and plan.
   - Tests and gates pass as required by the plan/constitution.

When to use:

- After `tasks.md` is generated and, ideally, after `/speckit.analyze`
  has validated coverage.

### 4.9 `/speckit.taskstoissues`

Template: `.specify/speckit.taskstoissues.md`

Purpose:

- Convert `tasks.md` into actionable, dependency‑aware GitHub issues
  using the GitHub MCP server or equivalent API integration.

High‑level flow:

1. Run `check-prerequisites.sh --json --require-tasks --include-tasks`
   to locate `tasks.md`.
2. Determine the Git remote:

   ```bash
   git config --get remote.origin.url
   ```

3. **Safety requirement:**
   - Only proceed if the remote is a **GitHub URL** matching the
     repository where issues will be created.
   - Never create issues in a different repository than the one pointed
     to by the remote.
4. For each task, use the configured MCP tool
   (`github/github-mcp-server/issue_write`) or equivalent GitHub API to
   create an issue that:
   - Captures the task description and file paths.
   - Retains task IDs and dependencies in the issue body or labels.
5. Process extension hooks `hooks.before_taskstoissues` and
   `hooks.after_taskstoissues` as in §3.2.

When to use:

- When the project wants to manage execution via GitHub issues rather
  than only via `tasks.md`.

---

## 5. Relationship to the `specify` CLI

The `specify` CLI is the project’s spec‑kit driver.

Key commands (already installed in this repo’s virtualenv):

- `specify init` – Initialize a new project.
  - **Do not** run this in this repository; it is already initialized.
- `specify check` – Verify that required tools are installed.
- `specify version` – Show CLI version and feature capabilities.
- `specify extension` – Manage spec‑kit extensions
  (installed/available/catalo gs).
- `specify preset` – Manage spec‑kit presets.
- `specify integration` – Manage coding‑agent integrations.
- `specify workflow` – Manage and run automation workflows defined in
  YAML.

For OpenHands agents working **inside this repo**:

- You typically **do not** need to call `specify init` or manipulate
  integrations/presets unless the user explicitly asks to change the
  spec‑kit installation.
- You _may_ use `specify check` or `specify version --features --json`
  to debug environment problems.
- Workflows (via `specify workflow run`) can be used when the user
  explicitly asks to run a particular automation; otherwise, prefer the
  direct speckit commands (invoked here as `./speckit.*`) and templates.

---

## 6. OpenHands‑specific expectations

When you are an OpenHands agent operating in this repo:

1. **Obey both doctrines**
   - The guidance in `AGENTS.md` (migration discipline, testing
     philosophy, RU behavior) and the spec‑kit doctrine here are both
     binding.
   - When they intersect, treat the constitution + spec‑kit documents as
     defining **what** to build, and the RU/OpenHands guidelines as
     defining **how** to build and validate it.

2. **Prefer spec‑kit flows for substantial work**
   - For new features or larger refactors, push work through the
     spec‑kit pipeline instead of jumping directly into ad‑hoc changes.
   - For small, clearly scoped fixes, you can work directly if the user
     prefers, but consider whether a lightweight spec + tasks pass would
     still add value.

3. **Use the project filesystem and shell**
   - When templates instruct you to run shell scripts, do so from the
     repo root.
   - Parse JSON outputs properly rather than re‑implementing path
     discovery.
   - Read and write files in the feature directory (`specs/<...>`) and
     `.specify/memory/` exactly as described.

4. **Respect safety and external‑service constraints**
   - For commands that talk to GitHub (e.g., `/speckit.taskstoissues`),
     use the repository’s configured credentials and obey the “remote
     must match” rule.
   - Never create issues, push commits, or modify external systems
     outside of what the user would reasonably expect from the invoked
     command.

5. **Keep artifacts synchronized**
   - When you update specs, plans, tasks, or checklists outside of a
     spec‑kit command, make sure you don’t break assumptions used by the
     templates (IDs, headings, directory structure, etc.).
   - Prefer invoking the appropriate `/speckit.*` command to make
     structural changes rather than editing these artifacts in ad‑hoc
     ways.

6. **Be explicit about which command you are executing**
   - When responding to a slash command, state near the top of your
     reply which `/speckit.*` command you are running and which key
     artifacts you will touch (e.g., `spec.md`, `plan.md`, `tasks.md`).
   - If you need to deviate from the template (e.g., missing files,
     security concerns), explain clearly what you are skipping and why.

By following this doctrine, agents will behave consistently within the
spec‑kit system, keep project artifacts synchronized, and still honor
RU’s existing migration, testing, and safety practices.
