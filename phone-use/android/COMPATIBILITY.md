# axdroid — device compatibility (measured)

## SM-G990B2 — Android 16 · 2026-10-26T00:57:52 · 9/9 passed

| gate | result | ms | evidence |
|---|---|---|---|
| G1 bridge | PASS | 33.1 |  |
| G2 tree | PASS | 1594.4 | app: com.android.settings · 1080×2340px · changes 7632 · overlay: com.android.systemui, com.sec.android.app.launcher, com.android.systemui · nodes=140 · clickable=10 |
| G3 semantic click | PASS | 10729.8 |  · via=ax-click-ancestor |
| G4 type+submit | PASS | 5663.2 | ACTION_SET_TEXT with device-side read-back verification · via=ax-set-text |
| G5 gesture fallback | PASS | 1574.0 | blind tap named 'com.android.settings.intelligence:id/his' and proceeded · via=gesture |
| G6 settle | PASS | 3132.4 |  · changes_after_home=7824 |
| G7 custody | PASS | 191.7 |  · refused_as=True |
| G8 guard | PASS | 4248.6 |  · target=Delete manage |
| G9 app sweep | PASS | 24114.2 | 6/6 apps expose a rich tree |

| app | quality | actionable | framework |
|---|---|---|---|
| Calendar (com.samsung.android.calendar) | rich | 10 | — |
| Camera (com.sec.android.app.camera) | rich | 23 | — |
| Gallery (com.sec.android.gallery3d) | rich | 12 | — |
| Messages (com.samsung.android.messaging) | rich | 15 | — |
| My Files (com.sec.android.app.myfiles) | rich | 11 | — |
| Contacts (com.samsung.android.app.contacts) | rich | 9 | — |
