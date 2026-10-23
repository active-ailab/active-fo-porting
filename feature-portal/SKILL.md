---
name: feature-portal
description: Use the local Feature Portal to evaluate feature-porting feasibility, compare Gerrit topic code with a local Zepp/Active workspace, port changes, build, flash, and run self-tests.
---

# Feature Portal

Use this skill when the user asks to operate, diagnose, extend, or evaluate the bundled Feature Portal. The complete runnable bundle is under:

`/home/hgw/workspace/feature-portal/scripts/feature-portal/`

The main executable entrypoint is:

`/home/hgw/workspace/feature-portal/scripts/feature-portal-start`

The original project copy is also compatible with `/home/hgw/monaco/tools/feature-portal`.

## Core workflow

1. Inspect the current workspace and service state before mutating code or repositories.
2. Start the bundled portal with `feature-portal-start --workspace /path/to/workspace` when the request is an interactive portal operation. Do not open `web/index.html` directly because API calls require the local server.
3. Establish the code baseline first: workspace path, manifest, branch, and build target. Treat baseline changes as invalidating previous evaluation results.
4. For code evaluation, prefer a Gerrit topic. The topic is fetched as Git objects and exported into `data/topic-src/`; do not cherry-pick or checkout merely to evaluate.
5. Compare topic code against the explicitly selected local workspace, not a remote branch or an unrelated default checkout.
6. Report document coverage, missing dependencies, hardware/config support, storage/map status, and estimated porting effort.
7. Only perform Gerrit download/cherry-pick, build, flash, or other state-changing actions when the user requested that stage.
8. Finish with verification appropriate to the stage: API health, evaluation response, download result, build log, flash result, or self-test checklist.

## Dependency evaluation invariant

The code evaluation must actively expose macros used by the topic but not defined in local code.

- Extract macros used by the topic changes, excluding macros defined by the topic itself.
- Search the selected local workspace for a real `#define` of each macro.
- A normal reference to the same identifier is not proof that the macro exists locally.
- Missing macros must appear in the evaluation missing-items list with type `宏`, including source file and line where the topic uses it.
- Preserve evidence for local definitions so the result can be audited.
- Also report missing included headers separately.

Read [references/feature-portal.md](references/feature-portal.md) for commands, API routes, result interpretation, and troubleshooting.

## Safety and scope

- Preserve existing user changes in the portal and workspaces.
- Do not run destructive baseline reset/sync commands without explicit confirmation immediately before the action; these can discard uncommitted repository changes.
- Keep the portal bound to localhost unless the user explicitly requests otherwise.
- Treat estimates as planning data; final compatibility requires build and device validation.
