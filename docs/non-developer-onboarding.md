# Axuse for non-developers

## What this is

Axuse lets your AI assistant use a real web browser for you: opening pages,
searching, reading results, filling in forms, collecting links and producing
an evidence-backed answer. It works for journalists, researchers, sales,
recruiting, operations and anyone else who uses Claude, ChatGPT or another AI
agent at work.

You do not need Git, a terminal, or the Axuse source code.

## One-time setup

Pick the app you use. Your agent should ask which supported browser and
profile you want to use, whether to install the browser extension, and
whether you want optional desktop control.

### Claude desktop app (Mac or Windows): one click

1. Download **axuse.mcpb** from https://ax-use.com (the "Add to Claude"
   button).
2. Double-click the file. The Claude app opens and asks to install it.
   Click **Install**. (If double-clicking does nothing: in Claude open
   Settings → Extensions → Advanced settings → Install Extension, and pick
   the file.)
3. Choose your browser and profile with your agent. For your existing
   signed-in Chrome or Edge profile, ask it to set up the axuse browser
   extension too, following https://ax-use.com/install.md. This is separate
   from the Claude desktop extension you just installed.
4. Fully quit and reopen Claude, then start a new chat so its tools load.
   If the agent cannot finish setup until after restarting, resume it then.

Claude includes everything axuse needs to run. You also need Google Chrome or
Microsoft Edge; Windows already has Edge.

### Codex, ChatGPT Work, Claude Code, Cursor or any other agent: one prompt

axuse does not work in the chatgpt.com chat window, which cannot install tools on
your computer. It works with the agents that run on your computer: Codex, ChatGPT Work, Claude Code, Claude Work, Meta Muse, OpenCode, Devin, Cursor, Kiro, Antigravity.

1. Copy the install prompt from https://ax-use.com/#prompt (the full text is
   also at https://ax-use.com/install.md).
2. Paste it into your agent (Codex, ChatGPT Work, Claude Code, Cursor, OpenCode and so on).
3. Choose your browser and default profile when asked. Your existing profile
   holds logins for sites such as Airbnb and Google. Choose the Chrome or
   Edge extension to use those sessions, or a separate profile to sign in
   independently. The agent installs Node.js if needed, downloads Axuse and
   registers it with your app.
4. Follow the agent's extension and permission instructions below, then
   fully quit and reopen the app or restart the CLI session. Most agents
   need this to load a newly added MCP server; a new chat alone may not be
   enough. If your app supports MCP reload, you can try that first.
5. Ask the agent to resume setup and verify `example.com` in your chosen
   browser and profile. Installation is complete only when that test works.

If your AI cannot fetch web pages, paste the full prompt text rather than
the link.

## Browser extension and permissions

For your existing Chrome or Edge profile, the agent registers the connection
and opens the extensions page. Switch to the chosen profile, open
`chrome://extensions` (Chrome) or `edge://extensions` (Edge), enable
**Developer mode**, click **Load unpacked**, and select the exact folder the
agent shows you. Keep that folder in place. The extension can control tabs
and use the profile's signed-in sessions; review any permission prompt. Keep
axuse enabled in only the selected running profile, because the bridge
currently connects one profile at a time.

Without the extension, a separate axuse profile starts signed out. Ask the
agent to make it persistent if you want its logins to survive future tasks.

On macOS, optional control of other desktop apps requires **System Settings →
Privacy & Security → Accessibility** and **Screen Recording** (sometimes
called **Screen & System Audio Recording**). Enable the app or executable
identified by the system prompt, then reopen it if requested. Browser-only
use does not require these desktop permissions. On Windows, follow any
actual host or browser permission prompts. Linux supports browser tools;
desktop control is not implemented.

## First test

Try a small browser request first:

```text
Use axuse to visit google.com and find the latest stock price of Apple (AAPL).
Include the currency, the quote's timestamp, and the source link.
```

You should see Axuse open or use the browser. If the assistant answers from
memory without opening Google, say:

```text
Use the connected Axuse browser for this task. Show me the pages you opened
and their URLs before summarizing the results.
```

## Useful research prompts

### A news brief

```text
Search the latest US news from the last 24 hours. Use Axuse to open the
relevant results, deduplicate repeated wire stories, and prioritize primary
sources.

Create a concise Markdown brief with:
- headline
- outlet and publication time
- URL
- two-sentence summary
- the main claim supported by the source
- whether the source is primary or secondary
- uncertainty, missing information, and conflicting reports

Do not publish anything or contact anyone. Tell me which pages were blocked,
paywalled, or unavailable instead of guessing.
```

### Collect structured information

```text
Find the official press releases published by [ORGANIZATION] about [TOPIC]
between [START DATE] and [END DATE]. Open each source with Axuse and return a
table containing date, title, URL, named people or organizations, locations,
and the exact facts supported by each page. Mark anything you could not verify.
```

### Compare claims

```text
Investigate the claim that [CLAIM]. Use Axuse to open at least five distinct
sources, including the most relevant primary source. Separate verified facts,
source interpretations, and unresolved claims. Include URLs and publication
dates for every item.
```

### Save a report

```text
Repeat the research above and save the final result as a Markdown report and
a CSV source list. Include the collection time, every URL opened, and a note
for each source that was blocked, paywalled, or unavailable.
```

## How to ask for better results

Include four things when they matter:

- **Topic:** what you are investigating.
- **Time window:** for example, “since yesterday” or exact dates.
- **Source rules:** official sources, local outlets, public records, or a
  specific list of publications.
- **Output:** a brief, timeline, table, source list, or Markdown/CSV files.

You do not need to describe browser clicks. Say what you want researched and
the assistant should choose the Axuse browser actions.

## Safety rules

Axuse should be used as a research assistant, not as an invisible publishing
agent. Ask for confirmation before it:

- logs into an account or handles private messages;
- submits a form or sends an email;
- downloads files;
- publishes, edits, or deletes content;
- contacts a source or another person.

It should record URLs and collection times, distinguish primary from secondary
sources, and say when a page could not be read. Never treat a search-result
snippet as proof when the underlying page is unavailable.

## If something goes wrong

**“Axuse is not available.”** Fully quit and reopen your agent app or restart
its CLI session, then start a new conversation. In Claude, check Settings → Extensions
that axuse is turned on. In other agents, paste: “Check that axuse is
installed and working, and fix it if not.”

**“No browser found.”** Install Google Chrome from
https://www.google.com/chrome/ and try again.

**A page is blocked or requires a login.** Ask Axuse to record the URL and
continue with accessible sources. Do not ask it to bypass a paywall, CAPTCHA,
or access control.

**The answer has no URLs.** Ask: “Redo this using Axuse, show every source URL
and publication date, and separate verified facts from your interpretation.”

## For administrators

- **Claude Team / Enterprise:** distribute `axuse.mcpb` through the
  organization's desktop-extension allowlist so members get it without
  downloading anything. Build it with `node tools/build-mcpb.mjs`.
- **ChatGPT Business / Enterprise:** members use the Codex app with the install
  prompt above. A one-click ChatGPT connector is in development.
- Business use requires a commercial license: dev@ax-use.com.
