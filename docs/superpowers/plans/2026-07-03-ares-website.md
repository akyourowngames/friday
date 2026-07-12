# Ares Website Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a dark-themed landing page for Ares with an interactive 3D robot hero, feature grid, install guide, and terminal demo — deployed to Vercel later.

**Architecture:** Next.js 14 App Router project with Tailwind CSS for styling, Framer Motion for scroll animations, and React Three Fiber for the 3D hero. Each section is its own component, composed in `page.tsx`. All styling uses Tailwind utility classes with CSS custom properties for the dark theme tokens.

**Tech Stack:** Next.js 14, React 18, TypeScript, Tailwind CSS 3, Framer Motion 11, React Three Fiber 8, Three.js, Lucide React

---

## File Map

| File | Purpose |
|------|---------|
| `ares-website/package.json` | Dependencies and scripts |
| `ares-website/tsconfig.json` | TypeScript config |
| `ares-website/next.config.js` | Next.js config (transpilePackages for R3F) |
| `ares-website/tailwind.config.ts` | Tailwind config with custom theme |
| `ares-website/postcss.config.js` | PostCSS config for Tailwind |
| `ares-website/app/globals.css` | Tailwind directives + CSS custom properties |
| `ares-website/app/layout.tsx` | Root layout, fonts, metadata |
| `ares-website/app/page.tsx` | Main page composing all sections |
| `ares-website/app/components/Navbar.tsx` | Sticky nav with scroll effect |
| `ares-website/app/components/Hero.tsx` | 3D robot hero + CTA |
| `ares-website/app/components/RobotModel.tsx` | Three.js robot component (inside Canvas) |
| `ares-website/app/components/Features.tsx` | 6-card feature grid |
| `ares-website/app/components/Install.tsx` | Install commands with copy |
| `ares-website/app/components/Demo.tsx` | Terminal typing animation |
| `ares-website/app/components/Footer.tsx` | Footer |
| `ares-website/public/models/robot.glb` | 3D robot model (downloaded from Sketchfab) |

---

## Task 1: Scaffold Next.js project

**Files:**
- Create: `ares-website/` (entire project)

- [ ] **Step 1: Create the Next.js project**

Run from the `friday/` directory:

```bash
cd /c/Users/anime/friday
npx create-next-app@latest ares-website --typescript --tailwind --eslint --app --src-dir=false --import-alias="@/*" --no-turbopack
```

When prompted, accept defaults (TypeScript: Yes, Tailwind: Yes, `src/` directory: No, App Router: Yes, import alias: `@/*`).

- [ ] **Step 2: Install additional dependencies**

```bash
cd ares-website
npm install three @react-three/fiber @react-three/drei @react-three/postprocessing framer-motion lucide-react
npm install -D @types/three
```

- [ ] **Step 3: Verify dev server starts**

```bash
npm run dev
```

Expected: Server starts on `http://localhost:3000`, default Next.js page renders.

- [ ] **Step 4: Commit**

```bash
git init
git add .
git commit -m "chore: scaffold Next.js project with Tailwind, R3F, Framer Motion"
```

---

## Task 2: Configure Tailwind and global styles

**Files:**
- Modify: `ares-website/tailwind.config.ts`
- Modify: `ares-website/app/globals.css`

- [ ] **Step 1: Update tailwind.config.ts with custom theme**

Replace the content of `ares-website/tailwind.config.ts`:

```typescript
import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        bg: {
          primary: "#000000",
          card: "#111111",
          elevated: "#1a1a1a",
        },
        border: {
          DEFAULT: "#222222",
          hover: "#333333",
        },
        text: {
          primary: "#ffffff",
          secondary: "#888888",
        },
        accent: {
          DEFAULT: "#06b6d4",
          glow: "rgba(6, 182, 212, 0.15)",
        },
      },
      fontFamily: {
        sans: ["var(--font-inter)", "system-ui", "sans-serif"],
        mono: ["var(--font-jetbrains)", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
```

- [ ] **Step 2: Update globals.css**

Replace the content of `ares-website/app/globals.css`:

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

:root {
  --bg-primary: #000000;
  --bg-card: #111111;
  --bg-elevated: #1a1a1a;
  --border: #222222;
  --border-hover: #333333;
  --text-primary: #ffffff;
  --text-secondary: #888888;
  --accent: #06b6d4;
  --accent-glow: rgba(6, 182, 212, 0.15);
}

html {
  scroll-behavior: smooth;
}

body {
  background-color: var(--bg-primary);
  color: var(--text-primary);
}

::selection {
  background-color: var(--accent);
  color: var(--bg-primary);
}
```

- [ ] **Step 3: Verify styles load**

Run dev server, confirm the page background is black and text is white.

- [ ] **Step 4: Commit**

```bash
git add tailwind.config.ts app/globals.css
git commit -m "feat: configure Tailwind theme tokens and global dark styles"
```

---

## Task 3: Set up root layout with fonts

**Files:**
- Modify: `ares-website/app/layout.tsx`

- [ ] **Step 1: Replace layout.tsx with font setup**

Replace the content of `ares-website/app/layout.tsx`:

```tsx
import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "./globals.css";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
});

const jetbrains = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains",
});

export const metadata: Metadata = {
  title: "Ares — Personal AI Assistant",
  description: "A terminal-based personal AI assistant that remembers everything about you. Think Jarvis, but in your terminal.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className="dark">
      <body className={`${inter.variable} ${jetbrains.variable} font-sans antialiased`}>
        {children}
      </body>
    </html>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add app/layout.tsx
git commit -m "feat: set up root layout with Inter and JetBrains Mono fonts"
```

---

## Task 4: Build Navbar component

**Files:**
- Create: `ares-website/app/components/Navbar.tsx`

- [ ] **Step 1: Create Navbar.tsx**

```tsx
"use client";

import { useState, useEffect } from "react";
import { motion } from "framer-motion";
import { Github } from "lucide-react";

const GITHUB_URL = "https://github.com/your-username/ares";

export default function Navbar() {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 50);
    window.addEventListener("scroll", onScroll);
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <motion.nav
      initial={{ y: -100 }}
      animate={{ y: 0 }}
      transition={{ duration: 0.5, ease: "easeOut" }}
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-300 ${
        scrolled
          ? "bg-black/80 backdrop-blur-md border-b border-border"
          : "bg-transparent"
      }`}
    >
      <div className="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
        <a href="#" className="text-xl font-bold text-text-primary">
          Ares
        </a>
        <a
          href={GITHUB_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="text-text-secondary hover:text-text-primary transition-colors"
          aria-label="View on GitHub"
        >
          <Github className="w-5 h-5" />
        </a>
      </div>
    </motion.nav>
  );
}
```

- [ ] **Step 2: Verify it renders**

Add `<Navbar />` to `app/page.tsx` temporarily, run dev server, confirm the nav appears and changes on scroll.

- [ ] **Step 3: Commit**

```bash
git add app/components/Navbar.tsx
git commit -m "feat: add Navbar with scroll effect and GitHub link"
```

---

## Task 5: Build Hero section with 3D robot

**Files:**
- Create: `ares-website/app/components/RobotModel.tsx`
- Create: `ares-website/app/components/Hero.tsx`
- Create: `ares-website/public/models/` (directory)

- [ ] **Step 1: Download a free robot model**

Search Sketchfab for a free `.glb` robot model (CC license). Good search terms: "robot", "android", "humanoid", "cyborg". Pick a sleek, futuristic one.

Download the `.glb` file and place it at:
```
ares-website/public/models/robot.glb
```

If you can't find one immediately, create a placeholder — we'll replace it later.

- [ ] **Step 2: Create RobotModel.tsx**

This is the Three.js component rendered inside the Canvas:

```tsx
"use client";

import { useRef } from "react";
import { useFrame } from "@react-three/fiber";
import { useGLTF } from "@react-three/drei";
import * as THREE from "three";

interface RobotModelProps {
  mouse: { x: number; y: number };
}

export default function RobotModel({ mouse }: RobotModelProps) {
  const group = useRef<THREE.Group>(null);
  const { scene } = useGLTF("/models/robot.glb");

  useFrame(() => {
    if (!group.current) return;
    // Gentle rotation based on mouse position
    group.current.rotation.y = THREE.MathUtils.lerp(
      group.current.rotation.y,
      mouse.x * 0.3,
      0.05
    );
    group.current.rotation.x = THREE.MathUtils.lerp(
      group.current.rotation.x,
      mouse.y * 0.15,
      0.05
    );
    // Subtle idle float
    group.current.position.y =
      Math.sin(Date.now() * 0.001) * 0.05;
  });

  return (
    <group ref={group}>
      <primitive object={scene} scale={1.5} position={[0, -1, 0]} />
    </group>
  );
}

useGLTF.preload("/models/robot.glb");
```

- [ ] **Step 3: Create Hero.tsx**

```tsx
"use client";

import { useState, useCallback, Suspense } from "react";
import { Canvas } from "@react-three/fiber";
import { Environment, Float } from "@react-three/drei";
import { EffectComposer, Bloom } from "@react-three/postprocessing";
import { motion } from "framer-motion";
import { ArrowRight, Github } from "lucide-react";
import RobotModel from "./RobotModel";

const GITHUB_URL = "https://github.com/your-username/ares";

const fadeUp = {
  hidden: { opacity: 0, y: 30 },
  visible: (i: number) => ({
    opacity: 1,
    y: 0,
    transition: { delay: i * 0.15, duration: 0.6, ease: "easeOut" },
  }),
};

export default function Hero() {
  const [mouse, setMouse] = useState({ x: 0, y: 0 });

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    const x = (e.clientX / window.innerWidth) * 2 - 1;
    const y = -(e.clientY / window.innerHeight) * 2 + 1;
    setMouse({ x, y });
  }, []);

  return (
    <section
      onMouseMove={handleMouseMove}
      className="relative min-h-screen flex items-center overflow-hidden"
    >
      {/* Background grid pattern */}
      <div
        className="absolute inset-0 opacity-[0.03]"
        style={{
          backgroundImage:
            "linear-gradient(rgba(255,255,255,0.1) 1px, transparent 1px), linear-gradient(90deg, rgba(255,255,255,0.1) 1px, transparent 1px)",
          backgroundSize: "60px 60px",
        }}
      />

      <div className="max-w-6xl mx-auto px-6 w-full grid grid-cols-1 lg:grid-cols-2 gap-12 items-center">
        {/* 3D Robot */}
        <div className="h-[400px] lg:h-[600px]">
          <Canvas camera={{ position: [0, 1, 5], fov: 45 }}>
            <ambientLight intensity={0.5} />
            <directionalLight position={[5, 5, 5]} intensity={1} />
            <Suspense fallback={null}>
              <Float speed={1.5} rotationIntensity={0.2} floatIntensity={0.5}>
                <RobotModel mouse={mouse} />
              </Float>
              <Environment preset="city" />
              <EffectComposer>
                <Bloom
                  luminanceThreshold={1}
                  luminanceSmoothing={0.9}
                  intensity={0.5}
                />
              </EffectComposer>
            </Suspense>
          </Canvas>
        </div>

        {/* Text content */}
        <div className="flex flex-col gap-6">
          <motion.h1
            custom={0}
            initial="hidden"
            animate="visible"
            variants={fadeUp}
            className="text-5xl md:text-7xl font-bold tracking-tight"
          >
            Ares
          </motion.h1>

          <motion.p
            custom={1}
            initial="hidden"
            animate="visible"
            variants={fadeUp}
            className="text-lg md:text-xl text-text-secondary max-w-md"
          >
            Your personal AI that remembers everything
          </motion.p>

          <motion.div
            custom={2}
            initial="hidden"
            animate="visible"
            variants={fadeUp}
            className="flex gap-4 mt-2"
          >
            <a
              href="#install"
              className="inline-flex items-center gap-2 bg-white text-black px-6 py-3 rounded-lg font-medium hover:bg-gray-200 transition-colors"
            >
              Get Started <ArrowRight className="w-4 h-4" />
            </a>
            <a
              href={GITHUB_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 border border-border px-6 py-3 rounded-lg font-medium text-text-secondary hover:text-text-primary hover:border-border-hover transition-colors"
            >
              <Github className="w-4 h-4" /> View on GitHub
            </a>
          </motion.div>
        </div>
      </div>
    </section>
  );
}
```

- [ ] **Step 4: Verify the 3D hero renders**

Run dev server, open `http://localhost:3000`. The robot model should render and respond to mouse movement. Bloom glow should be visible.

- [ ] **Step 5: Commit**

```bash
git add app/components/RobotModel.tsx app/components/Hero.tsx public/models/
git commit -m "feat: add Hero section with interactive 3D robot model"
```

---

## Task 6: Build Features section

**Files:**
- Create: `ares-website/app/components/Features.tsx`

- [ ] **Step 1: Create Features.tsx**

```tsx
"use client";

import { motion } from "framer-motion";
import { Brain, Mic, Monitor, Zap, Folder, Shield } from "lucide-react";

const features = [
  {
    icon: Brain,
    title: "Memory System",
    description: "Remembers facts, preferences, and context about you",
  },
  {
    icon: Mic,
    title: "Voice Mode",
    description: "Hands-free interaction with speech-to-text and text-to-speech",
  },
  {
    icon: Zap,
    title: "Skills System",
    description: "Reusable playbooks for any workflow — code review, research, and more",
  },
  {
    icon: Folder,
    title: "File Access",
    description: "Read, write, search, and edit files — all sandboxed locally",
  },
  {
    icon: Shield,
    title: "100% Private",
    description: "All your data stays on your machine. Zero cloud. Zero tracking.",
  },
];

const container = {
  hidden: {},
  visible: {
    transition: {
      staggerChildren: 0.1,
    },
  },
};

const item = {
  hidden: { opacity: 0, y: 20 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.5 } },
};

export default function Features() {
  return (
    <section className="py-24 px-6">
      <div className="max-w-6xl mx-auto">
        <h2 className="text-3xl md:text-4xl font-bold text-center mb-4">
          Everything you need
        </h2>
        <div className="w-16 h-px bg-accent mx-auto mb-16" />

        <motion.div
          variants={container}
          initial="hidden"
          whileInView="visible"
          viewport={{ once: true, margin: "-100px" }}
          className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6"
        >
          {features.map((feature) => (
            <motion.div
              key={feature.title}
              variants={item}
              className="group p-6 rounded-xl bg-bg-card border border-border hover:border-border-hover transition-all duration-300 hover:shadow-[0_0_30px_rgba(6,182,212,0.05)]"
            >
              <feature.icon className="w-8 h-8 text-accent mb-4" />
              <h3 className="text-lg font-semibold mb-2">{feature.title}</h3>
              <p className="text-text-secondary text-sm leading-relaxed">
                {feature.description}
              </p>
            </motion.div>
          ))}
        </motion.div>
      </div>
    </section>
  );
}
```

- [ ] **Step 2: Verify features render**

Run dev server, scroll to the features section. Cards should appear with staggered animation. Hover should show border glow.

- [ ] **Step 3: Commit**

```bash
git add app/components/Features.tsx
git commit -m "feat: add Features section with 6-card grid and scroll animations"
```

---

## Task 7: Build Install section

**Files:**
- Create: `ares-website/app/components/Install.tsx`

- [ ] **Step 1: Create Install.tsx**

```tsx
"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { Copy, Check } from "lucide-react";

const commands = [
  { label: "Install", command: "pip install -e ." },
  { label: "Run", command: "python -m ares" },
];

export default function Install() {
  return (
    <section id="install" className="py-24 px-6">
      <div className="max-w-4xl mx-auto">
        <h2 className="text-3xl md:text-4xl font-bold text-center mb-4">
          Get Started in Seconds
        </h2>
        <div className="w-16 h-px bg-accent mx-auto mb-16" />

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
          className="grid grid-cols-1 md:grid-cols-2 gap-6"
        >
          {commands.map((cmd) => (
            <CommandCard key={cmd.label} {...cmd} />
          ))}
        </motion.div>

        <motion.p
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true }}
          transition={{ delay: 0.3 }}
          className="text-center mt-10"
        >
          <a
            href="https://github.com/your-username/ares#readme"
            target="_blank"
            rel="noopener noreferrer"
            className="text-accent hover:underline text-sm"
          >
            Read the full documentation →
          </a>
        </motion.p>
      </div>
    </section>
  );
}

function CommandCard({ label, command }: { label: string; command: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(command);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="rounded-xl bg-bg-elevated/50 backdrop-blur-sm border border-border p-6">
      <p className="text-xs text-text-secondary uppercase tracking-wider mb-3">
        {label}
      </p>
      <div className="flex items-center justify-between gap-4">
        <code className="font-mono text-sm text-text-primary break-all">
          {command}
        </code>
        <button
          onClick={handleCopy}
          className="shrink-0 p-2 rounded-lg hover:bg-bg-card transition-colors text-text-secondary hover:text-text-primary"
          aria-label={`Copy ${command}`}
        >
          {copied ? (
            <Check className="w-4 h-4 text-green-400" />
          ) : (
            <Copy className="w-4 h-4" />
          )}
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Verify copy works**

Run dev server, click the copy button on each command. Clipboard should have the command text. Icon should change to checkmark for 2 seconds.

- [ ] **Step 3: Commit**

```bash
git add app/components/Install.tsx
git commit -m "feat: add Install section with copy-to-clipboard commands"
```

---

## Task 8: Build Demo (terminal) section

**Files:**
- Create: `ares-website/app/components/Demo.tsx`

- [ ] **Step 1: Create Demo.tsx**

```tsx
"use client";

import { useState, useEffect, useRef } from "react";
import { motion, useInView } from "framer-motion";
import { Github } from "lucide-react";

const GITHUB_URL = "https://github.com/your-username/ares";

const lines = [
  { type: "input", text: "> remember that I prefer dark mode" },
  { type: "output", text: "✓ Saved to memory #47" },
  { type: "gap" },
  { type: "input", text: "> remind me to call the dentist tomorrow at 2pm" },
  { type: "output", text: "✓ Reminder set for Jul 4, 2:00 PM" },
  { type: "gap" },
  { type: "input", text: "> what do you know about me?" },
  { type: "output", text: "Here's what I know:" },
  { type: "output", text: "  • Prefers dark mode" },
  { type: "output", text: "  • Working on Ares project" },
  { type: "output", text: "  • Uses Python + a local-first toolset" },
  { type: "output", text: "  • Wants a Jarvis-like assistant" },
];

const CHARS_PER_SECOND = 30;
const LINE_DELAY = 500;

export default function Demo() {
  const ref = useRef<HTMLDivElement>(null);
  const isInView = useInView(ref, { once: true, margin: "-100px" });
  const [visibleLines, setVisibleLines] = useState<number>(0);
  const [currentChar, setCurrentChar] = useState(0);
  const [typing, setTyping] = useState(false);

  useEffect(() => {
    if (!isInView) return;

    let lineIndex = 0;
    let charIndex = 0;
    let timeout: NodeJS.Timeout;

    const typeNextChar = () => {
      if (lineIndex >= lines.length) return;

      const line = lines[lineIndex];

      if (line.type === "gap") {
        lineIndex++;
        timeout = setTimeout(typeNextChar, LINE_DELAY);
        return;
      }

      if (line.type === "output") {
        // Output appears instantly
        setVisibleLines(lineIndex + 1);
        lineIndex++;
        timeout = setTimeout(typeNextChar, LINE_DELAY);
        return;
      }

      // Input types character by character
      setTyping(true);
      setVisibleLines(lineIndex + 1);
      setCurrentChar(charIndex + 1);
      charIndex++;

      if (charIndex < line.text.length) {
        timeout = setTimeout(typeNextChar, 1000 / CHARS_PER_SECOND);
      } else {
        setTyping(false);
        charIndex = 0;
        lineIndex++;
        timeout = setTimeout(typeNextChar, LINE_DELAY);
      }
    };

    timeout = setTimeout(typeNextChar, 500);
    return () => clearTimeout(timeout);
  }, [isInView]);

  return (
    <section className="py-24 px-6">
      <div className="max-w-3xl mx-auto">
        <h2 className="text-3xl md:text-4xl font-bold text-center mb-4">
          See it in action
        </h2>
        <div className="w-16 h-px bg-accent mx-auto mb-16" />

        <motion.div
          ref={ref}
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.6 }}
          className="rounded-xl overflow-hidden border border-border shadow-2xl"
        >
          {/* Title bar */}
          <div className="flex items-center gap-2 px-4 py-3 bg-bg-elevated border-b border-border">
            <div className="w-3 h-3 rounded-full bg-red-500/80" />
            <div className="w-3 h-3 rounded-full bg-yellow-500/80" />
            <div className="w-3 h-3 rounded-full bg-green-500/80" />
            <span className="ml-2 text-xs text-text-secondary font-mono">
              Ares Terminal
            </span>
          </div>

          {/* Terminal content */}
          <div className="bg-black p-6 font-mono text-sm min-h-[320px]">
            {lines.slice(0, visibleLines).map((line, i) => {
              if (line.type === "gap") return <div key={i} className="h-3" />;

              const isInput = line.type === "input";
              const isCurrentlyTyping =
                typing && i === visibleLines - 1 && isInput;

              return (
                <div
                  key={i}
                  className={`${isInput ? "text-text-primary" : "text-green-400"}`}
                >
                  {isCurrentlyTyping
                    ? line.text.slice(0, currentChar)
                    : line.text}
                  {isCurrentlyTyping && (
                    <span className="inline-block w-2 h-4 bg-white ml-0.5 animate-pulse" />
                  )}
                </div>
              );
            })}
            {visibleLines === 0 && (
              <div className="text-text-secondary">Loading...</div>
            )}
          </div>
        </motion.div>

        <motion.div
          initial={{ opacity: 0 }}
          whileInView={{ opacity: 1 }}
          viewport={{ once: true }}
          transition={{ delay: 0.4 }}
          className="text-center mt-10"
        >
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-2 border border-border px-6 py-3 rounded-lg font-medium text-text-secondary hover:text-text-primary hover:border-border-hover transition-colors"
          >
            <Github className="w-4 h-4" /> Try it yourself →
          </a>
        </motion.div>
      </div>
    </section>
  );
}
```

- [ ] **Step 2: Verify typing animation**

Run dev server, scroll to the demo section. The terminal should start typing automatically. Input lines type character-by-character, output lines appear instantly.

- [ ] **Step 3: Commit**

```bash
git add app/components/Demo.tsx
git commit -m "feat: add Demo section with terminal typing animation"
```

---

## Task 9: Build Footer

**Files:**
- Create: `ares-website/app/components/Footer.tsx`

- [ ] **Step 1: Create Footer.tsx**

```tsx
import { Github } from "lucide-react";

const GITHUB_URL = "https://github.com/your-username/ares";

export default function Footer() {
  return (
    <footer className="border-t border-border py-8 px-6">
      <div className="max-w-6xl mx-auto flex items-center justify-between">
        <p className="text-text-secondary text-sm">
          Ares © 2026
        </p>
        <a
          href={GITHUB_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="text-text-secondary hover:text-text-primary transition-colors"
          aria-label="View on GitHub"
        >
          <Github className="w-5 h-5" />
        </a>
      </div>
    </footer>
  );
}
```

- [ ] **Step 2: Commit**

```bash
git add app/components/Footer.tsx
git commit -m "feat: add Footer component"
```

---

## Task 10: Compose all sections in page.tsx

**Files:**
- Modify: `ares-website/app/page.tsx`

- [ ] **Step 1: Replace page.tsx**

```tsx
import Navbar from "./components/Navbar";
import Hero from "./components/Hero";
import Features from "./components/Features";
import Install from "./components/Install";
import Demo from "./components/Demo";
import Footer from "./components/Footer";

export default function Home() {
  return (
    <main className="bg-bg-primary min-h-screen">
      <Navbar />
      <Hero />
      <Features />
      <Install />
      <Demo />
      <Footer />
    </main>
  );
}
```

- [ ] **Step 2: Verify the full page**

Run dev server, scroll through all sections. Confirm:
- Navbar sticks and changes on scroll
- 3D robot renders and responds to mouse
- Feature cards animate in with stagger
- Install commands copy correctly
- Terminal types when scrolled into view
- Footer renders at the bottom

- [ ] **Step 3: Commit**

```bash
git add app/page.tsx
git commit -m "feat: compose all sections into landing page"
```

---

## Task 11: Update next.config.js for R3F

**Files:**
- Modify: `ares-website/next.config.js` or `ares-website/next.config.mjs`

- [ ] **Step 1: Update Next.js config**

```javascript
/** @type {import('next').NextConfig} */
const nextConfig = {
  transpilePackages: ["three"],
  reactStrictMode: true,
};

export default nextConfig;
```

Note: If the file is `next.config.js` (CommonJS), convert to `next.config.mjs` (ESM) or use `module.exports`. The `transpilePackages` ensures Three.js works with Next.js.

- [ ] **Step 2: Restart dev server and verify**

Stop and restart `npm run dev`. Confirm no build errors.

- [ ] **Step 3: Commit**

```bash
git add next.config.*
git commit -m "fix: configure Next.js to transpile Three.js"
```

---

## Task 12: Final polish and responsive check

**Files:**
- Modify: `ares-website/app/components/*.tsx` (if needed)
- Modify: `ares-website/app/globals.css` (if needed)

- [ ] **Step 1: Check mobile responsiveness**

Open Chrome DevTools, toggle device toolbar, test at:
- iPhone SE (375px)
- iPad (768px)
- Desktop (1440px)

Fix any layout issues:
- Hero: stack vertically on mobile, 3D canvas full width
- Features: single column on mobile
- Install: stacked cards on mobile
- Terminal: horizontal scroll or scaled font on small screens

- [ ] **Step 2: Check dark theme consistency**

Scroll through the entire page. Confirm:
- No white/light backgrounds leaking through
- All text is readable (proper contrast)
- Cards have consistent dark styling
- Borders are subtle (#222)

- [ ] **Step 3: Check animations**

- Hero fade-in on page load
- Feature cards stagger on scroll
- Install section fade-in on scroll
- Terminal typing triggers on scroll
- Navbar background changes on scroll

- [ ] **Step 4: Final commit**

```bash
git add .
git commit -m "feat: final polish and responsive fixes"
```

---

## Task 13: Test build for production

**Files:**
- None (verification only)

- [ ] **Step 1: Run production build**

```bash
npm run build
```

Expected: Build succeeds with no errors. Warnings about client components are fine.

- [ ] **Step 2: Preview production build**

```bash
npm run start
```

Open `http://localhost:3000`, verify everything works in production mode.

- [ ] **Step 3: Final commit**

```bash
git add .
git commit -m "chore: verify production build"
```

---

## Summary

| Task | Component | Key Feature |
|------|-----------|-------------|
| 1 | Project scaffold | Next.js + all deps |
| 2 | Tailwind + CSS | Dark theme tokens |
| 3 | Layout | Fonts (Inter, JetBrains) |
| 4 | Navbar | Scroll effect |
| 5 | Hero | 3D robot + R3F |
| 6 | Features | 6-card grid |
| 7 | Install | Copy commands |
| 8 | Demo | Typing animation |
| 9 | Footer | Minimal footer |
| 10 | page.tsx | Compose everything |
| 11 | next.config | R3F transpile fix |
| 12 | Polish | Responsive + theme check |
| 13 | Build test | Production build verify |

**Total: 13 tasks, ~60 steps**
