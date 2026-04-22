# AGENTS.md

## Project
- **Name:** Solomon's Usage Tracker
- **Stack:** Single-file HTML, CSS, and vanilla JavaScript
- **Run:** static file or `python3 -m http.server 5200`

## Architecture
- Main app lives in `index.html`
- No backend
- Data persists in `localStorage`
- Charts use Canvas API

## Build & Verify
```bash
python3 -m http.server 5200
```

After changes, load the page and verify the edited flow manually in a browser.

## Key Rules
- Keep it dependency-free unless there is a very strong reason not to.
- Preserve local-first behavior and `localStorage` persistence.
- Do not add a backend for something the browser can handle.
- Keep the app lightweight and easy to open anywhere.

## Style Guide
- No em dashes. Ever.
- Maintain the existing theme-switching and practical utility focus.

## Git Rules
- Use conventional commits.
- Never add `Co-Authored-By` lines.
- Never mention AI tools or vendors in commit messages.
