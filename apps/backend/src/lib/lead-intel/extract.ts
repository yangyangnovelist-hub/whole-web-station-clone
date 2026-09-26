import { FREEMAIL_DOMAINS } from "../instant-quote/catalog"

export type PageSnapshot = {
  url: string
  title: string | null
  description: string | null
  site_name: string | null
  headings: string[]
  text: string
  social_links: string[]
  organization: Record<string, unknown> | null
}

const ENTITIES: Record<string, string> = {
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": '"',
  "&#39;": "'",
  "&#039;": "'",
  "&apos;": "'",
  "&nbsp;": " ",
  "&ndash;": "-",
  "&mdash;": "-",
  "&rsquo;": "'",
  "&lsquo;": "'",
  "&rdquo;": '"',
  "&ldquo;": '"',
  "&reg;": "®",
  "&trade;": "™",
  "&copy;": "©",
}

export function decodeEntities(value: string) {
  return value
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_, code) =>
      String.fromCodePoint(parseInt(code, 16))
    )
    .replace(
      /&[a-z]+;|&#0?39;/gi,
      (entity) => ENTITIES[entity.toLowerCase()] ?? entity
    )
}

function clean(value: string | undefined | null, max = 400) {
  if (!value) {
    return null
  }
  const text = decodeEntities(value.replace(/<[^>]*>/g, " "))
    .replace(/\s+/g, " ")
    .trim()
  return text ? text.slice(0, max) : null
}

function metaContent(html: string, key: string) {
  const patterns = [
    new RegExp(
      `<meta[^>]+(?:name|property)=["']${key}["'][^>]*content=["']([^"']*)["']`,
      "i"
    ),
    new RegExp(
      `<meta[^>]+content=["']([^"']*)["'][^>]*(?:name|property)=["']${key}["']`,
      "i"
    ),
  ]
  for (const pattern of patterns) {
    const match = html.match(pattern)
    if (match) {
      return clean(match[1])
    }
  }
  return null
}

export function extractPageSnapshot(html: string, url: string): PageSnapshot {
  const withoutNoise = html
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<noscript[\s\S]*?<\/noscript>/gi, " ")
    .replace(/<svg[\s\S]*?<\/svg>/gi, " ")
    .replace(/<(nav|footer|header)[\s\S]*?<\/\1>/gi, " ")

  const headings = Array.from(
    withoutNoise.matchAll(/<h[1-3][^>]*>([\s\S]*?)<\/h[1-3]>/gi)
  )
    .map((match) => clean(match[1], 160))
    .filter((heading): heading is string => Boolean(heading))
    .slice(0, 12)

  const socialLinks = Array.from(
    new Set(
      Array.from(
        html.matchAll(
          /href=["'](https?:\/\/(?:www\.)?(?:instagram|facebook|linkedin|tiktok|x|twitter|youtube|pinterest)\.com\/[^"'#?]+)["']/gi
        )
      ).map((match) => match[1])
    )
  ).slice(0, 8)

  let organization: Record<string, unknown> | null = null
  for (const match of html.matchAll(
    /<script[^>]+type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi
  )) {
    try {
      const parsed = JSON.parse(match[1].trim())
      const nodes: unknown[] = Array.isArray(parsed)
        ? parsed
        : (parsed?.["@graph"] ?? [parsed])
      const org = nodes.find((node) => {
        const type = (node as Record<string, unknown>)?.["@type"]
        return (
          type === "Organization" ||
          type === "Corporation" ||
          type === "LocalBusiness" ||
          type === "Store" ||
          (Array.isArray(type) && type.includes("Organization"))
        )
      }) as Record<string, unknown> | undefined
      if (org) {
        organization = {
          name: org.name ?? null,
          description: org.description ?? null,
          url: org.url ?? null,
          sameAs: org.sameAs ?? null,
          address: org.address ?? null,
        }
        break
      }
    } catch {
      // ignore malformed JSON-LD
    }
  }

  const body =
    withoutNoise.match(/<body[^>]*>([\s\S]*)<\/body>/i)?.[1] ?? withoutNoise
  const text = (clean(body, 20000) ?? "").slice(0, 6000)

  return {
    url,
    title: clean(html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1], 200),
    description:
      metaContent(html, "description") ?? metaContent(html, "og:description"),
    site_name: metaContent(html, "og:site_name"),
    headings,
    text,
    social_links: socialLinks,
    organization,
  }
}

export function emailDomain(email: string) {
  const domain = email.split("@")[1]?.trim().toLowerCase()
  return domain || null
}

export function isFreemailDomain(domain: string | null) {
  return !domain || FREEMAIL_DOMAINS.has(domain)
}

/**
 * Picks the site to enrich: the website the buyer typed, else their business
 * email domain. Returns a normalized https URL or null.
 */
export function resolveCompanyWebsite(input: {
  website?: string | null
  email?: string | null
}) {
  const candidate = input.website?.trim()
  if (candidate) {
    const withProtocol = /^https?:\/\//i.test(candidate)
      ? candidate
      : `https://${candidate}`
    try {
      const url = new URL(withProtocol)
      if (url.hostname.includes(".")) {
        return `${url.protocol}//${url.hostname}/`
      }
    } catch {
      // fall through to the email domain
    }
  }

  const domain = input.email ? emailDomain(input.email) : null
  if (domain && !isFreemailDomain(domain)) {
    return `https://${domain}/`
  }

  return null
}
