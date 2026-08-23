# Calquity Visual Design System & Aesthetics Specification

> **Vibe:** Ultra-sleek institutional financial intelligence platform. Surreal, obsidian dark mode, hyper-precise monospaced metrics, micro-animations, cited superscripts, and glassmorphism.

---

## 🎨 1. CSS Custom Properties (`index.css` / Tailwind Config)

```css
:root {
  --bg-main: #080a0f;
  --bg-surface: #0f141f;
  --bg-card: #131b2e;
  --bg-card-hover: #1c2640;
  --border-subtle: rgba(255, 255, 255, 0.07);
  --border-active: rgba(56, 189, 248, 0.3);
  
  --text-main: #f8fafc;
  --text-muted: #94a3b8;
  --text-dim: #64748b;
  
  --accent-emerald: #10b981;  /* Verified / Citations / Current */
  --accent-emerald-glow: rgba(16, 185, 129, 0.15);
  --accent-cyan: #38bdf8;     /* Tool Executing / Active state */
  --accent-cyan-glow: rgba(56, 189, 248, 0.15);
  --accent-amber: #f59e0b;    /* Deprecated / Warning */
  --accent-rose: #ef4444;     /* Breach / Critical Escalation */
}

body {
  background-color: var(--bg-main);
  color: var(--text-main);
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  letter-spacing: -0.01em;
}

.mono-num {
  font-family: 'JetBrains Mono', monospace;
  font-variant-numeric: tabular-nums;
}

.calquity-glass {
  background: rgba(19, 27, 46, 0.7);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--border-subtle);
}

.calquity-glass-hover:hover {
  border-color: rgba(255, 255, 255, 0.15);
  background: rgba(28, 38, 64, 0.8);
}
```

---

## 💎 2. Key Visual Components

### Component A: Reasoning Pipeline Stream (Compass Style)
Shows real-time progress of the agent's thoughts:
```
[● 0.1s] Decomposing query "Can Northstar cancel ORD-1001 without fee?"
[● 1.2s] Querying Data Layer -> ORD-1001 status: BOOKED | Account: ACCT-001 (Northstar)
[● 0.8s] Searching Corpus -> Northstar Enterprise Agreement §4.2 & SOP v4
[● 0.4s] Resolving Policy vs Contract -> Contract §4.2 waives fee prior to pickup
[✓ 2.5s] Composing cited answer
```
- Active step blinks with Cyan glowing pulse `#38BDF8`.
- Timers use tabular monospace font.

### Component B: Citation Chip `[1]`, `[2]`
- Superscript emerald pill: `bg-[#10b981]/10 text-[#10b981] border border-[#10b981]/30 hover:bg-[#10b981]/20`
- Clicking opens **Source Inspector Slide-over**:
  - Highlights exact text snippet
  - Shows Document Title (e.g., `05_Northstar_Logistics_Enterprise_Agreement.pdf`)
  - Authority Badge: `1.0 (Enterprise Contract)`
  - Freshness Badge: `CURRENT`

### Component C: Data-Layer Access Scoping Indicator
Header bar shows current security context:
- `🔒 Security Context: Customer (Northstar Logistics / ACCT-001)`
- Tool outputs display `Enforced: WHERE account_id = 'ACCT-001'`
- Attempting to query ACCT-002 shows a sleek red security alert: `[ACCESS DENIED] Scope violation caught at data boundary.`

### Component D: Confirmation Drawer for Escalations
When agent triggers `ActionTool`:
- Card appears with a glowing Amber/Rose border:
  `⚠️ Action Required: Create Support Escalation for TKT-501 (P1 High Severity)`
- Includes `[Approve & Execute]` (Emerald) and `[Reject]` (Ghost slate) buttons.

### Component E: Proactive Issue Matrix (Internal View)
Grid of active operational anomalies:
- **Card 1 (Critical):** `TKT-501: All shipment creation failing (HTTP 500)` -> Affected: Northstar Logistics
- **Card 2 (Pattern):** `Bulk CSV Upload Failure (TKT-502 & TKT-451)` -> Affected: LumenWorks
- **Card 3 (SLA Risk):** `ORD-2002: Pickup Missed by Carrier RoadRunner (3 hrs late)` -> Credit SLA breach imminent

---

## 🎯 3. Color Codes for Statuses

| Entity / State | Color Code | Tailwind Class |
|---|---|---|
| Enterprise Contract / Current Policy | Emerald | `bg-emerald-500/10 text-emerald-400 border-emerald-500/30` |
| Deprecated Policy (v2) | Amber | `bg-amber-500/10 text-amber-400 border-amber-500/30` |
| Historical Ticket (Context Only) | Slate Muted | `bg-slate-500/10 text-slate-400 border-slate-500/30` |
| Active Tool Execution | Cyan | `bg-sky-500/10 text-sky-400 border-sky-500/30 animate-pulse` |
| P1 Urgent Alert / SLA Breach | Crimson | `bg-red-500/10 text-red-400 border-red-500/30` |
