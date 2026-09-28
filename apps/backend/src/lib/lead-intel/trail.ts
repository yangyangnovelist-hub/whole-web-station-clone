export type TrailEvent = {
  type: string
  url?: string | null
  path?: string | null
  title?: string | null
  referrer?: string | null
  product_type?: string | null
  payload?: Record<string, unknown> | null
  created_at: string | Date
}

export type TrailSummary = {
  events: number
  pages_viewed: number
  first_seen: string | null
  last_seen: string | null
  active_minutes: number
  top_pages: { path: string; title: string | null; views: number }[]
  product_interest: { product_type: string; signals: number }[]
  quote_interactions: number
  entry_referrer: string | null
  utm: Record<string, string> | null
}

const ACTIVE_GAP_MS = 30 * 60 * 1000

export function summarizeTrail(events: TrailEvent[]): TrailSummary {
  const sorted = [...events].sort(
    (a, b) =>
      new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
  )

  const pages = new Map<
    string,
    { path: string; title: string | null; views: number }
  >()
  const interest = new Map<string, number>()
  let quoteInteractions = 0
  let entryReferrer: string | null = null
  let utm: Record<string, string> | null = null
  let activeMs = 0

  sorted.forEach((event, index) => {
    if (event.type === "page_view" && event.path) {
      const page = pages.get(event.path) ?? {
        path: event.path,
        title: event.title ?? null,
        views: 0,
      }
      page.views++
      pages.set(event.path, page)
    }
    if (event.type.startsWith("quote_")) {
      quoteInteractions++
    }
    if (event.product_type) {
      const weight = event.type.startsWith("quote_") ? 3 : 1
      interest.set(
        event.product_type,
        (interest.get(event.product_type) ?? 0) + weight
      )
    }
    if (!entryReferrer && event.referrer) {
      entryReferrer = event.referrer
    }
    const eventUtm = event.payload?.utm
    if (!utm && eventUtm && typeof eventUtm === "object") {
      utm = eventUtm as Record<string, string>
    }
    if (index > 0) {
      const gap =
        new Date(event.created_at).getTime() -
        new Date(sorted[index - 1].created_at).getTime()
      if (gap > 0 && gap < ACTIVE_GAP_MS) {
        activeMs += gap
      }
    }
  })

  const iso = (value: string | Date | undefined) =>
    value ? new Date(value).toISOString() : null

  return {
    events: sorted.length,
    pages_viewed: Array.from(pages.values()).reduce(
      (sum, page) => sum + page.views,
      0
    ),
    first_seen: iso(sorted[0]?.created_at),
    last_seen: iso(sorted[sorted.length - 1]?.created_at),
    active_minutes: Math.round(activeMs / 60000),
    top_pages: Array.from(pages.values())
      .sort((a, b) => b.views - a.views)
      .slice(0, 8),
    product_interest: Array.from(interest.entries())
      .map(([product_type, signals]) => ({ product_type, signals }))
      .sort((a, b) => b.signals - a.signals)
      .slice(0, 5),
    quote_interactions: quoteInteractions,
    entry_referrer: entryReferrer,
    utm,
  }
}
