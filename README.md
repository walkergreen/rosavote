# RosaVote

Code-authenticated, multi-chapter election infrastructure for democratic
membership organizations — built around the rules DSA chapters actually use:
ballots, voter codes, Scottish STV tabulation (with
quota-constrained leadership counts), audited administration, and
independently verifiable results.

> **Disclaimer:** RosaVote is an independent open-source project built by DSA
> members. It is **not affiliated with, endorsed by, or an official project of
> the Democratic Socialists of America (DSA)** or of OpaVote.

Flask + Firestore on Cloud Run. See `CLAUDE.md` for architecture and
operations; `tools/smoke_test.py` runs the full offline test suite
(no GCP credentials needed).

## License

Copyright © 2026 Walker Green.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. If you run a modified version of this software as a network
service, you must offer its users the corresponding source code.

See [LICENSE](LICENSE) for the full text. Election transparency is the point:
anyone operating a modified RosaVote must let its voters see the code that
counts their ballots.
