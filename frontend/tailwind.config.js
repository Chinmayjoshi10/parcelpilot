/**
 * CalQuity design tokens, read from the live site rather than invented.
 *
 * The earlier palette here was a guess — dark, terminal, cyan-and-emerald.
 * calquity.com is the opposite: a warm off-white ground, near-black ink, a
 * single muted slate-blue accent, and forest green reserved for the one idea
 * their whole product rests on — a claim traced to its source.
 *
 * Sampled from computed styles on the live page:
 *   ground          rgb(254,253,251)   the warm paper, not #fff
 *   ink             rgb(10,10,11)      near-black, a hair warm
 *   accent          rgb(58,92,120)     slate blue, their only accent (100 uses)
 *   cited           rgb(46,125,91)     forest green, used sparingly
 *   muted / faint   rgb(113,113,119) / rgb(194,194,197)
 *   surface tint    rgb(243,242,238)
 *   headings        Hanken Grotesk, weight 500, tracking -0.045em
 *
 * Names stay by ROLE, not by colour — which is what made this swap a config
 * change rather than a find-and-replace across every component. `verified`
 * still means verified; only its hex moved.
 */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Grounds, lightest to deepest.
        base: '#FEFDFB',      // the page itself
        surface: '#FFFFFF',   // cards lift by being pure white on warm paper
        raised: '#F3F2EE',    // inset panels, hover states
        sunk: '#EDEBE6',      // wells, code blocks

        edge: '#E3E1DB',      // hairline borders
        'edge-strong': '#D2CFC7',

        ink: '#0A0A0B',
        muted: '#5A5A5E',
        faint: '#8E8E95',

        // Semantic. `verified` is the product's whole thesis, so it gets the
        // green CalQuity reserves for cited answers.
        verified: '#2E7D5B',
        active: '#3A5C78',    // the slate blue: tool calls, focus, primary action
        warn: '#9A6B1F',      // deprecated sources, conflicts — legible on paper
        breach: '#A3342A',    // P1, SLA breach
      },
      fontFamily: {
        // Hanken Grotesk is CalQuity's face. Loaded from Google Fonts, with a
        // real fallback stack so a blocked font request degrades rather than
        // silently substituting something else.
        sans: ['"Hanken Grotesk"', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      letterSpacing: {
        // Their headings run notably tight: -2.61px on 58px.
        display: '-0.045em',
      },
      boxShadow: {
        // On a light ground a card lifts by shadow, not by a lighter fill.
        card: '0 1px 2px rgba(10,10,11,.04), 0 8px 24px rgba(10,10,11,.05)',
        pill: '0 1px 2px rgba(10,10,11,.06), 0 12px 32px rgba(10,10,11,.08)',
      },
      backgroundImage: {
        // The faint dot grid behind their hero. Their most distinctive
        // background signature and nearly free to reproduce.
        grid: 'radial-gradient(rgba(10,10,11,.055) 1px, transparent 1px)',
      },
      backgroundSize: {
        grid: '22px 22px',
      },
    },
  },
  plugins: [],
}
