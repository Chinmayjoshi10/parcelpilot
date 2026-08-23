/**
 * Calquity design tokens.
 *
 * Named by ROLE rather than by colour, so the palette can shift without a
 * find-and-replace across components: `verified` stays meaningful,
 * `emerald-400` would not.
 */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        base: '#080A0F',
        surface: '#0F141F',
        raised: '#131B2E',
        edge: '#1E293B',
        ink: '#F8FAFC',
        muted: '#94A3B8',
        verified: '#10B981',
        active: '#38BDF8',
        warn: '#F59E0B',
        breach: '#EF4444',
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      boxShadow: {
        glass: '0 1px 0 0 rgba(255,255,255,0.04) inset, 0 8px 32px rgba(0,0,0,0.4)',
      },
    },
  },
  plugins: [],
}
