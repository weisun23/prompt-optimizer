# Public example: intent classification and offline failure scoring

[中文](README.md) · [Repository overview](../../README.en.md)

This walkthrough uses **hand-authored inputs and synthetic output fixtures**. It contains no customer data or credentials and makes no model calls. It demonstrates the scorer; it is not a benchmark or evidence of prompt improvement.

## Starting prompt and hypothesis

[prompt_v1.md](prompt_v1.md) asks for one of four intent labels but leaves overlapping cancellation and refund requests undefined. The example contract assigns `refund` when the user explicitly asks for paid money back, including when they also request cancellation.

[prompt_v2.md](prompt_v2.md) adds that boundary rule. It is an **untested candidate**, not an accepted prompt. Both prompts are in Chinese to match the dataset. A real comparison requires executing both with the same production model and reviewed case set.

## Run the existing scorer

From the repository root, with Python 3:

```bash
python -c "import shutil; shutil.copytree('examples/intent-classification/recordings', '.demo-output/recordings', dirs_exist_ok=True)"
cd .demo-output
python ../bin/scorer.py --config ../examples/intent-classification/config.offline.json --task ../examples/intent-classification/task.json --cases ../examples/intent-classification/dataset.json --trajectories recordings --out scores/offline --manifest run_manifest.json
cd ..
```

The [dataset](dataset.json) contains four cases. The [fixtures](recordings/) produce two PASS results, one FAIL result for the overlap boundary, and one INVALID result for a synthetic execution timeout. Expected valid-case accuracy is 66.7% (2/3), with zero judge calls. This is a property of the synthetic fixtures, not a model accuracy measurement.

Generate an HTML scoring report:

```bash
python bin/report_gen.py .demo-output --rounds offline --card-round offline --trajectories recordings --out .demo-output/report.html --user-requested
```

Open `.demo-output/report.html` in a browser. The report has no final accepted prompt because no candidate execution or acceptance decision took place. Generated artifacts stay in the ignored `.demo-output/` directory.

The [checked scorer output](offline-result.json) is included so you can compare your local reproduction. It describes synthetic fixtures only.

## Move to real evaluation

Create an independent workspace, configure the production model, and ask the host agent to review the task contract, cases, budget, and success criteria. Use `expected` scoring and execute the starting prompt and candidate against the same cases. Keep synthetic fixtures separate from real model trajectories.

These four teaching cases do not constitute comprehensive coverage. Add cancellation-only, negated refund, historical refund mentions, overlap, and normal consultation cases as appropriate. Rebuild the baseline after changing labels, repeat focused tests, protect previously correct behavior, and run a comparable full regression before accepting a candidate.
