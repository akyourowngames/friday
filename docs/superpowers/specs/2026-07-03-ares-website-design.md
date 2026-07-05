# Ares Website — Landing Page Design Spec

## Overview

A dark-themed landing page for Ares (personal AI assistant) with an interactive 3D hero, feature showcase, installation guide, and a simulated terminal demo. Separate project from the Ares codebase. Deployed to Vercel later.

**Tech Stack:** Next.js 14 (App Router) + Tailwind CSS + Framer Motion + React Three Fiber

**Hosting:** Local dev now → Vercel later

**Aesthetic:** Minimal dark / sleek — deep blacks, subtle gradients, clean typography, smooth animations. Apple-like polish.

---

## Project Structure

```
ares-website/
├── app/
│   ├── layout.tsx              # Root layout, fonts (Inter), dark theme provider
│   ├── page.tsx                # Main landing page — composes all sections
│   ├── globals.css             # Tailwind directives + custom CSS variables
│   └── components/
│       ├── Navbar.tsx          # Sticky top nav: logo, links, GitHub icon
│       ├── Hero.tsx            # Full-viewport hero with 3D robot + CTA buttons
│       ├── Features.tsx        # 2x3 feature card grid
│       ├── Install.tsx         # Two-step install commands with copy buttons
│       ├── Demo.tsx            # Fake terminal with typing animation
│       └── Footer.tsx          # Footer with links, GitHub, copyright
├── public/
│   └── models/
│       └── robot.glb           # 3D robot model file
├── tailwind.config.ts          # Extended dark theme colors
├── next.config.js              # Next.js config
├── tsconfig.json
├── package.json
└── README.md
```

---

## Dependencies

```json
{
  "dependencies": {
    "next": "^14.2.0",
    "react": "^18.3.0",
    "react-dom": "^18.3.0",
    "@react-three/fiber": "^8.15.0",
    "@react-three/drei": "^9.88.0",
    "@react-three/postprocessing": "^2.15.0",
    "three": "^0.160.0",
    "framer-motion": "^11.0.0",
    "lucide-react": "^0.300.0"
  },
  "devDependencies": {
    "tailwindcss": "^3.4.0",
    "autoprefixer": "^10.4.0",
    "postcss": "^8.4.0",
    "typescript": "^5.3.0",
    "@types/react": "^18.2.0",
    "@types/three": "^0.160.0"
  }
}
```

---

## Sections

### 1. Navbar

- Fixed/sticky at top, transparent → solid black on scroll
- Left: "Ares" logo text (bold, Inter font)
- Right: GitHub icon link, optional "Docs" link
- Subtle border-bottom on scroll (Framer Motion `useScroll`)

### 2. Hero Section

**Layout:** Full viewport height (`min-h-screen`), deep black background (`#000` or `#0a0a0a`).

**Left/Center:** Interactive 3D robot model
- React Three Fiber `<Canvas>` wrapping a `.glb` robot model
- **Model source:** Find a free sleek humanoid robot `.glb` from Sketchfab (CC license) during implementation. Search terms: "robot", "android", "humanoid" — pick one that looks futuristic/minimal. Place in `public/models/robot.glb`.
- Model slowly rotates, responds to mouse/cursor position (subtle parallax)
- Post-processing: bloom effect for edge glow
- Fallback: if model fails to load, display a CSS-animated robot silhouette
- The 3D canvas takes ~50% of the viewport on desktop, full width on mobile

**Right/Center:** Text content
- **Title:** "Ares" — large, bold, Inter font, white
- **Tagline:** *"Your personal AI that remembers everything"* — muted gray (`#888`)
- **Two buttons:**
  - "Get Started" — filled button, scrolls to Install section
  - "View on GitHub" — outline/ghost button, opens GitHub repo in new tab

**Background:** Subtle grid pattern or very faint scan lines for depth. No heavy effects.

**Animations:** Framer Motion — fade-in + slide-up on page load, staggered for title/tagline/buttons.

### 3. Features Section

**Layout:** Max-width container, 2-column grid on tablet, 3-column on desktop, single column on mobile. Section title: "Everything you need" with a subtle divider line.

**6 Feature Cards:**

| # | Feature | Icon (Lucide) | Description |
|---|---------|---------------|-------------|
| 1 | Memory System | `Brain` | Remembers facts, preferences, and context about you |
| 2 | Voice Mode | `Mic` | Hands-free interaction with speech-to-text and text-to-speech |
| 3 | Desktop App | `Monitor` | Beautiful Electron + React interface with streaming chat |
| 4 | Skills System | `Zap` | Reusable playbooks for any workflow — code review, research, and more |
| 5 | File Access | `Folder` | Read, write, search, and edit files — all sandboxed locally |
| 6 | 100% Private | `Shield` | All your data stays on your machine. Zero cloud. Zero tracking. |

**Card Styling:**
- Dark card background (`#111` or `#1a1a1a`)
- Subtle 1px border (`#222`), rounded corners (`rounded-xl`)
- On hover: border brightens slightly, subtle glow/shadow
- Icon in a muted accent color (soft cyan `#06b6d4` or similar)
- Feature name in white, description in muted gray

**Animations:** Framer Motion — cards reveal on scroll with staggered delay (`staggerChildren: 0.1`).

### 4. Install / Quick Start Section

**Layout:** Centered content, section title: "Get Started in Seconds"

**Two command cards side by side (stacked on mobile):**

**Card 1 — Install**
```bash
pip install -e .
```

**Card 2 — Run**
```bash
python -m ares
```

**Card Styling:**
- Glassmorphism background (slight blur, semi-transparent dark bg)
- Monospace font for commands (`JetBrains Mono` or `Fira Code`)
- Each card has a **copy button** (clipboard icon) — on click, copies command and shows "Copied!" for 2 seconds
- Subtle label above each command: "Install" / "Run"

**Below commands:** Text link — "Read the full documentation →" pointing to the GitHub README.

### 5. Live Demo Section

**Layout:** Centered, section title: "See it in action"

**Terminal window:**
- Dark terminal container with rounded corners and a subtle shadow
- **Title bar:** Three dots (red/yellow/green) + "Ares Terminal" text
- **Content:** Simulated chat that auto-types when scrolled into view

**Simulated conversation:**
```
> remember that I prefer dark mode
✓ Saved to memory #47

> remind me to call the dentist tomorrow at 2pm
✓ Reminder set for Jul 4, 2:00 PM

> what do you know about me?
Here's what I know:
  • Prefers dark mode
  • Working on Ares project
  • Uses Python + Electron
  • Wants a Jarvis-like assistant
```

**Typing animation:**
- Each line types out character by character with a blinking cursor
- Framer Motion `useInView` triggers the animation when the section scrolls into view
- Delay between lines for natural pacing (~500ms gap)
- Output lines (✓ responses) appear instantly after the user input finishes typing

**Below terminal:** "Try it yourself →" button linking to GitHub repo.

### 6. Footer

- Simple, minimal
- Left: "Ares" text + "© 2026"
- Right: GitHub icon link
- Subtle top border (`#222`)
- Dark background matching the rest of the page

---

## Design Tokens

| Token | Value | Usage |
|-------|-------|-------|
| `--bg-primary` | `#000000` | Page background |
| `--bg-card` | `#111111` | Card backgrounds |
| `--bg-elevated` | `#1a1a1a` | Elevated surfaces |
| `--border` | `#222222` | Borders |
| `--border-hover` | `#333333` | Border on hover |
| `--text-primary` | `#ffffff` | Headings, primary text |
| `--text-secondary` | `#888888` | Descriptions, muted text |
| `--accent` | `#06b6d4` | Icons, links, highlights (cyan) |
| `--accent-glow` | `rgba(6, 182, 212, 0.15)` | Glow effects |

**Fonts:**
- Headings + body: Inter (Google Fonts)
- Code/terminal: JetBrains Mono (Google Fonts)

---

## Responsive Breakpoints

| Breakpoint | Layout |
|------------|--------|
| Mobile (< 640px) | Single column, stacked sections, full-width 3D canvas |
| Tablet (640–1024px) | 2-column feature grid, side-by-side install cards |
| Desktop (> 1024px) | 3-column feature grid, hero split layout, max-width 1200px |

---

## Performance Considerations

- **3D model:** Lazy-load the `.glb` file, show a placeholder/skeleton while loading
- **Framer Motion:** Use `LazyMotion` + `domAnimation` to reduce bundle size
- **Images/Icons:** Use Lucide (tree-shakeable) instead of full icon sets
- **Fonts:** Use `next/font` for optimized font loading (no layout shift)
- **3D canvas:** Only mount when the hero section is in view (optional: `React.lazy`)

---

## Success Criteria

1. Page loads in < 3 seconds on a 3G connection
2. 3D robot renders smoothly at 60fps on modern devices
3. All sections are responsive (mobile → desktop)
4. Copy-to-clipboard works for install commands
5. Terminal typing animation triggers on scroll
6. GitHub link opens correctly
7. Clean, minimal dark aesthetic — no visual clutter
