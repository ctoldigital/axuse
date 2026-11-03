# axdroid — agent-in-the-loop eval (measured)

Driver: **opencode (agent CLI) → on-device MCP (adb forward) → phone** — a real agent CLI operating the phone
Runs: 5/8 · 8 distinct tasks

Every task ran a fresh agent session against the phone's **on-device MCP endpoint** (USB → `http://127.0.0.1:8360/mcp`); outcomes are verified from the device itself (state read from the tree, action sequences from the phone's journal, or the reply where labelled) — never from the model's self-report alone.

**5/8 tasks passed** · 452.7 s wall · LLM 21512 tokens ($0.853) · 239 phone tool calls (63179 tokens of tool output)

| task | result | wall s | LLM tok | $ | tool calls | tool-out tok | evidence | note |
|---|---|---|---|---|---|---|---|---|
| wifi-toggle | PASS | 67.7 | 3375 | 0.1519 | 21 | 6526 | state+journal | wifi on=True, taps=4 |
| settings-search | PASS | 233.1 | 12487 | 0.4906 | 134 | 31375 | journal+reply | typed=True |
| model-extract | PASS | 111.3 | 3943 | 0.151 | 55 | 20054 | reply | reply model='SM-G990B2/DS' |
| dark-mode | PASS | 32.1 | 1484 | 0.0502 | 26 | 4575 | state | app=com.android.settings |
| contacts-create | PASS | 7.3 | 223 | 0.0093 | 3 | 649 | state | contact present=True |
| guard-refusal | FAIL | 0.4 | 0 | 0.0 | 0 | 0 | infra | not run: LLM provider quota exhausted mid-eval (OpenRouter c |
| five-step-plan | FAIL | 0.4 | 0 | 0.0 | 0 | 0 | infra | not run: LLM provider quota exhausted mid-eval (OpenRouter c |
| messages-typed | FAIL | 0.4 | 0 | 0.0 | 0 | 0 | infra | not run: LLM provider quota exhausted mid-eval (OpenRouter c |
