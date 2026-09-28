import {
  extractPageSnapshot,
  PageSnapshot,
  resolveCompanyWebsite,
} from "./extract"
import { isAllowedByRobots, safeFetch } from "./safe-fetch"

/**
 * Reads the buyer's public company homepage (from the website they typed or
 * their business email domain) to enrich the lead. Honors robots.txt and
 * returns null on any failure.
 */
export async function crawlCompanySite(
  input: { website?: string | null; email?: string | null },
  logger?: { warn: (message: string) => void }
): Promise<PageSnapshot | null> {
  const target = resolveCompanyWebsite(input)
  if (!target) {
    return null
  }

  try {
    const origin = new URL(target).origin
    try {
      const robots = await safeFetch(`${origin}/robots.txt`, {
        timeoutMs: 3000,
        maxBytes: 100_000,
        accept: "text/plain,*/*;q=0.5",
      })
      if (robots.status === 200 && !isAllowedByRobots(robots.body, "/")) {
        return null
      }
    } catch {
      // no robots.txt (or unreachable): treat as allowed
    }

    const page = await safeFetch(target)
    if (page.status >= 400 || !/html/i.test(page.contentType)) {
      return null
    }
    return extractPageSnapshot(page.body, page.url)
  } catch (error) {
    logger?.warn(`[lead] could not read ${target}: ${(error as Error).message}`)
    return null
  }
}
