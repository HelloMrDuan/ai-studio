# Third-Party Notices

Xiaoduan Studio V3 intentionally tracks and reuses mature open-source work where license terms allow it.

## MoneyPrinterTurbo

- Repository: https://github.com/harry0703/MoneyPrinterTurbo
- License: MIT
- Reviewed upstream baseline: `5ceffd02a267de2ede0bbdb0fab8d7d875ea9842`
- Status in this foundation commit: architecture/service boundaries reviewed; no MoneyPrinterTurbo source file has yet been copied verbatim into this commit.
- Future ports must preserve the upstream copyright and MIT license notice and record the upstream path/commit in the corresponding V3 migration documentation.

## waoowaoo

- Repository: https://github.com/waooAI/waoowaoo
- License: Elastic License 2.0
- Reviewed upstream baseline: `6cbbe22cc6492159e0f649d507e4e21a9aec3074`
- Status in this foundation commit: architecture and contracts studied, especially Creative Skills and Temporal durable execution. V3 foundation code in this commit is independently implemented and is not a verbatim copy of waoowaoo source.
- Any later direct reuse must be separately reviewed against Elastic License 2.0 and must preserve required notices and modification disclosures.

The tracked baselines are machine-readable in `config/upstreams.json` and are checked by `.github/workflows/upstream-watch.yml`.
