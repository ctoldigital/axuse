# axdroid — task benchmark (measured)

## 2026-10-26T00:58:55 · 8/8 passed, 1 skipped, 0 failed · 74.7s total · 19591 tokens of tool output

| task | result | ms | tool calls | output tokens |
|---|---|---|---|---|
| wifi toggle round-trip | PASS | 3977 | 4 | 1837 |
| settings search + filter | PASS | 5855 | 4 | 2781 |
| about-phone data extraction | PASS | 10682 | 3 | 2135 |
| navigate to Dark mode | PASS | 5241 | 3 | 2886 |
| contacts: create + verify | PASS | 14717 | 6 | 4060 |
| play store search | skip | 5636 | 2 | 1323 |
| guard refuses 'Reset' | PASS | 18614 | 4 | 1975 |
| 5-step plan (phone_run) | PASS | 3813 | 1 | 700 |
| messages: typed draft | PASS | 6203 | 3 | 1894 |

home-button action latency: 592 ms, 603 ms, 606 ms, 645 ms (p50 ≈ 606 ms incl. settle)

## Observe token economy (same screens, compact vs verbose render)

| screen | nodes | compact | verbose | observe ms |
|---|---|---|---|---|
| launcher | 134 | 659t | 1207t | 56 |
| settings root | 134 | 827t | 1372t | 34 |
| settings subpage (About phone) | 161 | 1095t | 1641t | 438 |
| messages list | 97 | 506t | 1051t | 55 |
| camera list | 152 | 1189t | 1734t | 52 |

Median observe: **827 tokens** compact vs 1372 verbose — a 40% cut. Method: tokens = ceil(chars/4).

### Action outputs vs full re-dump (same navigation)

| action | output tokens | ms |
|---|---|---|
| tap Connections | 790t | 736 |
| back | 794t | 546 |
| observe (after-action alternative: full re-dump) | 780t | 47 |
