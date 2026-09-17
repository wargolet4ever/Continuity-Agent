# Identity benchmark

This directory is reserved for local identity calibration manifests and their
labelled image crops. Real character material is intentionally not committed
to the public repository.

## Labels

- `match`: the configured character is clearly the same identity despite an
  acceptable change in pose, expression, lighting, or framing.
- `mismatch`: the subject is clearly a different identity or the generated
  character has drifted enough that regeneration is required.
- `ambiguous`: the crop is too occluded, distant, blurred, or borderline for
  an automatic decision and should be sent to human review.

Do not label technical image corruption, missing characters, costume drift, or
prop deformation as identity mismatch. Those belong to separate continuity
rules.

## Split and leakage rules

Every case must use either the `calibration` or `validation` split. Thresholds
are selected from calibration cases only. Validation cases measure the selected
thresholds and never influence them.

Frames from one generated take or one source clip must stay in the same split.
Near-duplicate frames must not be copied across splits. The manifest validator
also rejects an exact repeated image-pair and crop combination.

For a report to be marked `ready`, all assets must be real project material and
the benchmark must contain at least:

| Split | Match | Ambiguous | Mismatch |
| --- | ---: | ---: | ---: |
| Calibration | 20 | 10 | 20 |
| Validation | 10 | 5 | 10 |

Both splits must also reach macro recall of at least `0.80`, and validation
must contain no cross-extreme errors (`mismatch` predicted as `match`, or
`match` predicted as `mismatch`).

Smaller or synthetic datasets still produce score distributions and may produce
a threshold recommendation, but the report remains `provisional`.

## Manifest shape

The machine-readable contract is
`schemas/identity-benchmark-v1.schema.json`. Asset paths are resolved relative
to the manifest. Crops use normalized `[left, top, right, bottom]` coordinates.

```json
{
  "contract_version": "1.0",
  "benchmark_id": "passenger-zero-identity-v1",
  "source_kind": "real",
  "description": "Labelled character crops from approved and rejected takes",
  "cases": [
    {
      "id": "daniel-shot19-take02-frame0042",
      "label": "match",
      "split": "calibration",
      "reference_path": "assets/daniel-reference.png",
      "candidate_path": "assets/daniel-shot19-frame0042.png",
      "reference_crop": [0.1, 0.05, 0.9, 0.95],
      "candidate_crop": [0.55, 0.1, 0.9, 0.8],
      "character_id": "daniel",
      "shot_id": "19",
      "notes": "Approved take, three-quarter view"
    }
  ]
}
```

## Run calibration

From the repository root on Windows PowerShell:

```powershell
python identity_calibration.py benchmarks/identity/benchmark.json `
  --output benchmarks/identity/calibration-report.json
```

The report includes a dataset fingerprint, per-label score distributions,
confusion matrices, readiness reasons, and recommended `reject_below` and
`review_below` values. The command never edits `canon.json`; thresholds must be
reviewed before being copied into a character's `identity_reference` config.
