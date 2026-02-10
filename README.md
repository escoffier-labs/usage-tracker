<p align="center">
  <img src="https://img.shields.io/badge/HTML5-E34F26?style=flat-square&logo=html5&logoColor=white" alt="HTML5" />
  <img src="https://img.shields.io/badge/CSS3-1572B6?style=flat-square&logo=css3&logoColor=white" alt="CSS3" />
  <img src="https://img.shields.io/badge/JavaScript-F7DF1E?style=flat-square&logo=javascript&logoColor=black" alt="JavaScript" />
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="MIT License" />
</p>

# 💰 Solomon's Usage Tracker

**AI model usage and cost tracker with visual theme switching.**

Monitor spending across AI providers, calculate ROI per session, and track costs over time. A single HTML file with zero dependencies, five visual themes, and localStorage persistence.

## Quick Start

```bash
git clone https://github.com/solomonneas/usage-tracker.git
cd usage-tracker
python3 -m http.server 5200
```

Open [http://localhost:5200](http://localhost:5200) or just open `index.html` directly.

## Features

- 💵 Track costs across multiple AI providers (OpenAI, Anthropic, Google, etc.)
- 📊 Per-session cost breakdown with input/output token counts
- 📈 Spending trends over time with interactive charts
- 🧮 ROI calculator comparing cost vs. time saved
- 🎨 Five visual themes (dark, light, cyberpunk, ocean, forest)
- 💾 All data stored in localStorage, no backend needed
- 📤 Export data as JSON for backup or analysis
- 🔍 Filter and search by provider, model, or date range
- ⚡ Zero dependencies, single index.html file

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Markup | HTML5 | Page structure |
| Styling | CSS3 | Themes and responsive layout |
| Logic | Vanilla JavaScript | All interactivity and calculations |
| Storage | localStorage | Client-side data persistence |
| Charts | Canvas API | Spending visualizations |

## Why This Exists

AI API costs add up fast and most providers only show billing at the account level. Usage Tracker gives you granular, per-session visibility into what you are spending and whether the investment is paying off.

## License

MIT
