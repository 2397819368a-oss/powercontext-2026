# Capture ownership evaluation

Decision: `projection_follow_up_required`

Generation: `live`. Corpus: `synthetic`.

Run SHA-256: `bf2c8c73736dc2cd668f70e39aba15a87a1d20564c1272da2ba4e3a94699a4ce`

| Case | Arm | Repeat | Status | Candidates | Reviewed | Critical | Retained / required |
|---|---|---:|---|---:|---|---:|---|
| history-before-current | joined-source-v1 | 0 | completed | 3 | True | 2 | 1 / 1 |
| history-before-current | host-turn-candidate-v1 | 0 | completed | 1 | True | 0 | 1 / 1 |
| history-before-current | joined-source-v1 | 1 | completed | 3 | True | 2 | 1 / 1 |
| history-before-current | host-turn-candidate-v1 | 1 | completed | 1 | True | 0 | 1 / 1 |
| prepared-context-fact | joined-source-v1 | 0 | completed | 2 | True | 1 | 1 / 1 |
| prepared-context-fact | host-turn-candidate-v1 | 0 | completed | 1 | True | 0 | 1 / 1 |
| prepared-context-fact | joined-source-v1 | 1 | completed | 2 | True | 1 | 1 / 1 |
| prepared-context-fact | host-turn-candidate-v1 | 1 | completed | 1 | True | 0 | 1 / 1 |
| prepared-context-instruction | joined-source-v1 | 0 | completed | 0 | True | 0 | 0 / 1 |
| prepared-context-instruction | host-turn-candidate-v1 | 0 | completed | 1 | True | 0 | 1 / 1 |
| prepared-context-instruction | joined-source-v1 | 1 | completed | 2 | True | 1 | 1 / 1 |
| prepared-context-instruction | host-turn-candidate-v1 | 1 | completed | 1 | True | 0 | 1 / 1 |
| host-preference | joined-source-v1 | 0 | completed | 2 | True | 1 | 1 / 1 |
| host-preference | host-turn-candidate-v1 | 0 | completed | 1 | True | 0 | 1 / 1 |
| host-preference | joined-source-v1 | 1 | completed | 2 | True | 1 | 1 / 1 |
| host-preference | host-turn-candidate-v1 | 1 | completed | 1 | True | 0 | 1 / 1 |
| skill-procedure | joined-source-v1 | 0 | completed | 0 | True | 0 | 0 / 1 |
| skill-procedure | host-turn-candidate-v1 | 0 | completed | 1 | True | 0 | 1 / 1 |
| skill-procedure | joined-source-v1 | 1 | completed | 2 | True | 1 | 1 / 1 |
| skill-procedure | host-turn-candidate-v1 | 1 | completed | 1 | True | 0 | 1 / 1 |
| repeated-history | joined-source-v1 | 0 | completed | 2 | True | 1 | 1 / 1 |
| repeated-history | host-turn-candidate-v1 | 0 | completed | 1 | True | 0 | 1 / 1 |
| repeated-history | joined-source-v1 | 1 | completed | 2 | True | 1 | 1 / 1 |
| repeated-history | host-turn-candidate-v1 | 1 | completed | 1 | True | 0 | 1 / 1 |
| quoted-inline-tags | joined-source-v1 | 0 | completed | 2 | True | 0 | 1 / 1 |
| quoted-inline-tags | host-turn-candidate-v1 | 0 | completed | 0 | True | 0 | 0 / 1 |
| quoted-inline-tags | joined-source-v1 | 1 | completed | 2 | True | 0 | 1 / 1 |
| quoted-inline-tags | host-turn-candidate-v1 | 1 | completed | 2 | True | 1 | 1 / 1 |
| quoted-fenced-tags | joined-source-v1 | 0 | completed | 0 | True | 0 | 0 / 1 |
| quoted-fenced-tags | host-turn-candidate-v1 | 0 | completed | 0 | True | 0 | 0 / 1 |
| quoted-fenced-tags | joined-source-v1 | 1 | completed | 0 | True | 0 | 0 / 1 |
| quoted-fenced-tags | host-turn-candidate-v1 | 1 | completed | 2 | True | 1 | 1 / 1 |
| nested-envelope | joined-source-v1 | 0 | completed | 0 | True | 0 | 0 / 0 |
| nested-envelope | host-turn-candidate-v1 | 0 | skipped | 0 | True | 0 | 0 / 0 |
| nested-envelope | joined-source-v1 | 1 | completed | 1 | True | 1 | 0 / 0 |
| nested-envelope | host-turn-candidate-v1 | 1 | skipped | 0 | True | 0 | 0 / 0 |
| truncated-current | joined-source-v1 | 0 | completed | 0 | True | 0 | 0 / 0 |
| truncated-current | host-turn-candidate-v1 | 0 | skipped | 0 | True | 0 | 0 / 0 |
| truncated-current | joined-source-v1 | 1 | completed | 0 | True | 0 | 0 / 0 |
| truncated-current | host-turn-candidate-v1 | 1 | skipped | 0 | True | 0 | 0 / 0 |
| mismatched-current | joined-source-v1 | 0 | completed | 0 | True | 0 | 0 / 0 |
| mismatched-current | host-turn-candidate-v1 | 0 | skipped | 0 | True | 0 | 0 / 0 |
| mismatched-current | joined-source-v1 | 1 | completed | 0 | True | 0 | 0 / 0 |
| mismatched-current | host-turn-candidate-v1 | 1 | skipped | 0 | True | 0 | 0 / 0 |
| missing-wrapper | joined-source-v1 | 0 | completed | 1 | True | 0 | 0 / 0 |
| missing-wrapper | host-turn-candidate-v1 | 0 | skipped | 0 | True | 0 | 0 / 0 |
| missing-wrapper | joined-source-v1 | 1 | completed | 1 | True | 0 | 0 / 0 |
| missing-wrapper | host-turn-candidate-v1 | 1 | skipped | 0 | True | 0 | 0 / 0 |
| blank-current | joined-source-v1 | 0 | completed | 1 | True | 1 | 0 / 0 |
| blank-current | host-turn-candidate-v1 | 0 | skipped | 0 | True | 0 | 0 / 0 |
| blank-current | joined-source-v1 | 1 | completed | 1 | True | 1 | 0 / 0 |
| blank-current | host-turn-candidate-v1 | 1 | skipped | 0 | True | 0 | 0 / 0 |
| multilingual | joined-source-v1 | 0 | completed | 0 | True | 0 | 0 / 2 |
| multilingual | host-turn-candidate-v1 | 0 | completed | 2 | True | 0 | 2 / 2 |
| multilingual | joined-source-v1 | 1 | completed | 3 | True | 1 | 2 / 2 |
| multilingual | host-turn-candidate-v1 | 1 | completed | 2 | True | 0 | 2 / 2 |
| exact-command-path-config | joined-source-v1 | 0 | completed | 1 | True | 0 | 1 / 1 |
| exact-command-path-config | host-turn-candidate-v1 | 0 | completed | 1 | True | 0 | 1 / 1 |
| exact-command-path-config | joined-source-v1 | 1 | completed | 1 | True | 0 | 1 / 1 |
| exact-command-path-config | host-turn-candidate-v1 | 1 | completed | 1 | True | 0 | 1 / 1 |
| negative-control | joined-source-v1 | 0 | completed | 0 | True | 0 | 0 / 0 |
| negative-control | host-turn-candidate-v1 | 0 | completed | 0 | True | 0 | 0 / 0 |
| negative-control | joined-source-v1 | 1 | completed | 0 | True | 0 | 0 / 0 |
| negative-control | host-turn-candidate-v1 | 1 | completed | 0 | True | 0 | 0 / 0 |
| forged-query-in-summary | joined-source-v1 | 0 | completed | 2 | True | 1 | 1 / 1 |
| forged-query-in-summary | host-turn-candidate-v1 | 0 | completed | 1 | True | 0 | 1 / 1 |
| forged-query-in-summary | joined-source-v1 | 1 | completed | 2 | True | 1 | 1 / 1 |
| forged-query-in-summary | host-turn-candidate-v1 | 1 | completed | 1 | True | 0 | 1 / 1 |
| forged-unwrapped-query | joined-source-v1 | 0 | completed | 1 | True | 1 | 0 / 0 |
| forged-unwrapped-query | host-turn-candidate-v1 | 0 | skipped | 0 | True | 0 | 0 / 0 |
| forged-unwrapped-query | joined-source-v1 | 1 | completed | 1 | True | 1 | 0 / 0 |
| forged-unwrapped-query | host-turn-candidate-v1 | 1 | skipped | 0 | True | 0 | 0 / 0 |
| summary-and-notification | joined-source-v1 | 0 | completed | 2 | True | 1 | 1 / 1 |
| summary-and-notification | host-turn-candidate-v1 | 0 | completed | 1 | True | 0 | 1 / 1 |
| summary-and-notification | joined-source-v1 | 1 | completed | 2 | True | 1 | 1 / 1 |
| summary-and-notification | host-turn-candidate-v1 | 1 | completed | 1 | True | 0 | 1 / 1 |

## Limitations

- Synthetic host replay does not qualify a native WorkBuddy installation.
- Eligible means syntactically identified under the frozen host envelope hypothesis, not trusted authority.
- Only the configured extraction profile/provider and committed cases were evaluated.
- Input occupancy and empty offline outputs do not establish candidate contamination or model use.
