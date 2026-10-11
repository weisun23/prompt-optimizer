# Prompt Optimizer — Evaluation-driven Prompt Optimization Skill

A **Claude Code and Codex Agent Skill** for prompt evaluation, failure analysis, tool-calling validation, and regression-tested optimization.

[中文](README.md) · [Offline example](examples/intent-classification/README.en.md) · [Agent instructions](SKILL.md)

License: [MIT](LICENSE).

Version: **1.4.11**. The host agent investigates failures and edits prompts; configured models perform only their assigned execution, generation, simulation, or judging roles.

## What it helps you do

- Evaluate classification, structured extraction, fixed JSON, conversational agents, and free-text tasks.
- Score exact outputs with deterministic Python comparisons instead of an LLM judge.
- Validate tool names, call counts, order, timing, and arguments.
- Separate model failures from invalid cases, execution errors, and scoring problems.
- Test one root cause at a time against failing cases, neighboring protection cases, and a full regression set.
- Track prompt versions, model configuration, case snapshots, hashes, and acceptance decisions.
- Define task-specific case categories and stability rules rather than imposing universal severity levels.

The optimization workflow is: establish a baseline → inspect failures → propose one targeted change → repeat focused tests → run a comparable full regression → accept or roll back using task-specific criteria.

## Install

With Node.js and the Skills CLI:

```bash
npx skills add weisun23/prompt-optimizer --skill prompt-optimizer
```

Choose a host explicitly:

```bash
npx skills add weisun23/prompt-optimizer --skill prompt-optimizer --agent claude-code
npx skills add weisun23/prompt-optimizer --skill prompt-optimizer --agent codex
```

The default scope is the current project. Add `--global` for user-wide installation. Preview discovery without installation:

```bash
npx skills add weisun23/prompt-optimizer --list
```

Alternatively, copy the **whole repository** into a supported skill directory, such as `~/.claude/skills/prompt-optimizer/` for Claude Code or `~/.agents/skills/prompt-optimizer/` for a shared installation. Copying only `SKILL.md` omits required scripts and references. Restart the host or open a fresh session and verify skill discovery.

CC Switch is an optional shared manager, not a dependency. Keep one canonical installation and avoid overlapping managers. This repository does not migrate an existing local installation.

## Try the scorer without an API key

Requires Python 3; the bundled scripts use the standard library.

```bash
git clone https://github.com/weisun23/prompt-optimizer.git
cd prompt-optimizer
python -c "import shutil; shutil.copytree('examples/intent-classification/recordings', '.demo-output/recordings', dirs_exist_ok=True)"
cd .demo-output
python ../bin/scorer.py --config ../examples/intent-classification/config.offline.json --task ../examples/intent-classification/task.json --cases ../examples/intent-classification/dataset.json --trajectories recordings --out scores/offline --manifest run_manifest.json
cd ..
```

The [public walkthrough](examples/intent-classification/README.en.md) includes a starting prompt, an untested candidate, four hand-authored cases, and synthetic output fixtures. It demonstrates PASS / FAIL / INVALID scoring with **zero model calls**. These fixtures are not evidence of model performance or prompt improvement.

## Start a real optimization task

1. Install the skill and select an independent workspace.
2. Copy `config.example.json` into the workspace and configure the model roles required for your task. Replace `YOUR_*` placeholders and supply credentials through the named environment variables.
3. Provide your prompt, production model, evaluation rules, resource budget, and success criteria to the host agent.
4. Review the generated cases and task contracts before execution.

Example request:

```text
Use prompt-optimizer to evaluate and optimize the intent classifier in
examples/intent-classification/prompt_v1.md using dataset.json as reviewed seed cases.
Create an independent workspace, use my configured production model, and confirm
the evaluation rules and budget before making API calls. Use deterministic
expected-output scoring. Evaluate prompt_v2.md as an untested candidate.
```

In Codex, the explicit invocation is `$prompt-optimizer`. The current skill instructions communicate with the user in Chinese; this English README does not change that runtime behavior.

To resume, ask the host to read `OPTIMIZATION_SUMMARY.md` and verify the run manifest, task, cases, model configuration, and version decisions. Changed evaluation inputs require a comparable new baseline.

## Scoring modes

| Mode | Typical task | LLM judge |
|---|---|---|
| `expected` | Classification, extraction, fixed JSON | No |
| `expected-semantic` | Semantic equivalence to a reference | Yes |
| `judge` | Task-specific subjective criteria | Yes |
| `tool-binary` | Tool-call contracts | Only explicitly semantic arguments |

## Evidence and limits

Runs retain prompts, tool definitions, case snapshots, SHA-256 hashes, trajectories, scores, and decisions. Invalid cases or device errors are excluded from model accuracy. Changes to a model, case set, scoring contract, or production mode invalidate direct comparisons.

The skill does not guarantee improvement. Results depend on authoritative labels, coverage, and model capabilities; repeated evaluation and human review remain necessary. Real execution incurs model API costs. The offline example incurs none.

## Reference documentation

The detailed runtime references are currently in Chinese:

- [Execution contracts](references/execution-contract.md)
- [Scoring and optimization loop](references/prompt-optimization-loop.md)
- [Scoring and run manifests](references/scoring-and-manifest.md)
- [Failure triage and case review](references/failure-triage-and-case-review.md)
- [Model profile adaptation](references/model-profile-adaptation.md)

For development verification, run `python -m unittest discover -s tests` from the repository root. Code tests verify the implementation; they do not establish prompt optimization gains.
