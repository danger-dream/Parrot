import { ui } from '@/stores/ui'

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}

export interface ChartColors {
  blue: string
  green: string
  orange: string
  red: string
  purple: string
  teal: string
  yellow: string
  pink: string
  indigo: string
  grid: string
  axis: string
  text: string
  text2: string
  tooltipBg: string
  tooltipBorder: string
  series: string[]
}

export function chartColors(): ChartColors {
  // Read after the theme attribute flips so both palettes stay in sync with CSS.
  void ui.dark
  const c = {
    blue: cssVar('--blue'),
    green: cssVar('--green'),
    orange: cssVar('--orange'),
    red: cssVar('--red'),
    purple: cssVar('--purple'),
    teal: cssVar('--teal'),
    yellow: cssVar('--yellow'),
    pink: cssVar('--pink'),
    indigo: cssVar('--indigo'),
    grid: cssVar('--chart-grid'),
    axis: cssVar('--chart-axis'),
    text: cssVar('--text'),
    text2: cssVar('--text-2'),
    tooltipBg: ui.dark ? 'rgba(40,40,44,0.94)' : 'rgba(255,255,255,0.96)',
    tooltipBorder: ui.dark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.08)',
  }
  return { ...c, series: [c.blue, c.teal, c.purple, c.orange, c.green, c.pink, c.indigo, c.yellow] }
}

export function tooltipBase(c: ChartColors) {
  return {
    backgroundColor: c.tooltipBg,
    borderColor: c.tooltipBorder,
    borderWidth: 0.5,
    padding: [8, 10],
    textStyle: { color: c.text, fontSize: 12, fontFamily: 'inherit' },
    extraCssText: 'border-radius:10px;box-shadow:0 10px 30px -10px rgba(0,0,0,.45);backdrop-filter:blur(20px);',
  }
}

export function axisBase(c: ChartColors) {
  return {
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: c.axis, fontSize: 10.5, fontFamily: 'inherit' },
    splitLine: { lineStyle: { color: c.grid } },
  }
}
