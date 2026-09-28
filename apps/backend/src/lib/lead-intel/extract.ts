import { FREEMAIL_DOMAINS } from "../instant-quote/catalog"

export type OrganizationSnapshot = {
  name: string | null
  description: string | null
  url: string | null
  sameAs: string[]
  address: string | null
}

export type PageSnapshot = {
  url: string
  title: string | null
  description: string | null
  site_name: string | null
  headings: string[]
  text: string
  social_links: string[]
  organization: OrganizationSnapshot | null
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

// Out-of-range references stay as text instead of throwing a RangeError.
const fromCodePoint = (entity: string, code: number) =>
  code <= 0x10ffff ? String.fromCodePoint(code) : entity

export function decodeEntities(value: string) {
  return value
    .replace(/&#(\d+);/g, (entity, code) => fromCodePoint(entity, Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (entity, code) =>
      fromCodePoint(entity, parseInt(code, 16))
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
  // [^<>] stops an unclosed "<" at the next "<", which keeps this linear;
  // "<!--" goes on its own since a comment often wraps markup.
  const text = decodeEntities(value.replace(/<[^<>]*>|<!--/g, " "))
    .replace(/\s+/g, " ")
    .trim()
  return text ? text.slice(0, max) : null
}

/*
 * The page is attacker-controlled (up to 750 KB), so nothing below uses a
 * regex that can backtrack across the document. Each scan finds a fixed
 * opening token, then indexOf-style searches forward for its end, and stops
 * as soon as an end is missing (no later element could close either).
 */

/** exec() from `from`; a match leaves lastIndex just after it. */
function findFrom(html: string, pattern: RegExp, from: number) {
  pattern.lastIndex = from
  return pattern.exec(html)
}

/** Removes <tag ...>...</tag> blocks, each ending at its first close. */
function stripElements(html: string, tags: string[]) {
  const open = new RegExp(`<(${tags.join("|")})`, "gi")
  const closers = new Map(
    tags.map((tag) => [tag, new RegExp(`</${tag}>`, "gi")] as const)
  )
  const unclosed = new Set<string>()
  let out = ""
  let pos = 0
  let match: RegExpExecArray | null
  while (unclosed.size < tags.length && (match = open.exec(html))) {
    const tag = match[1].toLowerCase()
    if (unclosed.has(tag)) {
      continue
    }
    const close = findFrom(html, closers.get(tag)!, open.lastIndex)
    if (!close) {
      unclosed.add(tag)
      continue
    }
    out += `${html.slice(pos, match.index)} `
    pos = open.lastIndex = close.index + close[0].length
  }
  return out + html.slice(pos)
}

/**
 * Inner HTML of the first <tag ...>...</tag>. With `lastClose` it runs to the
 * last closing tag instead (like a greedy match).
 */
function innerHtml(html: string, tag: string, lastClose = false) {
  const open = findFrom(html, new RegExp(`<${tag}`, "gi"), 0)
  if (!open) {
    return null
  }
  const gt = html.indexOf(">", open.index + tag.length + 1)
  if (gt === -1) {
    return null
  }
  const closer = new RegExp(`</${tag}>`, "gi")
  let close = findFrom(html, closer, gt + 1)
  if (!close) {
    return null
  }
  if (lastClose) {
    for (let next = closer.exec(html); next; next = closer.exec(html)) {
      close = next
    }
  }
  return html.slice(gt + 1, close.index)
}

const HEADING_OPEN = /<h[1-3]/gi
const HEADING_CLOSE = /<\/h[1-3]>/gi

function extractHeadings(html: string) {
  const headings: string[] = []
  let pos = 0
  while (headings.length < 12) {
    const open = findFrom(html, HEADING_OPEN, pos)
    const gt = open ? html.indexOf(">", HEADING_OPEN.lastIndex) : -1
    const close = gt === -1 ? null : findFrom(html, HEADING_CLOSE, gt + 1)
    if (!close) {
      break
    }
    const heading = clean(html.slice(gt + 1, close.index), 160)
    if (heading) {
      headings.push(heading)
    }
    pos = HEADING_CLOSE.lastIndex
  }
  return headings
}

const ATTR_GAP = /[\t\n\f\r /]*/y
const ATTR_NAME = /[^\t\n\f\r />][^\t\n\f\r />=]*/y
const ATTR_EQUALS = /[\t\n\f\r ]*=[\t\n\f\r ]*/y
const ATTR_UNQUOTED = /[^\t\n\f\r >]*/y

/**
 * Reads a tag's attributes from just after its name, the way an HTML
 * tokenizer does. Returns null when the tag (or a quoted value) never closes:
 * it then swallows the rest of the page, so there is nothing left to scan.
 */
function readAttributes(html: string, from: number) {
  const attrs = new Map<string, string>()
  let i = from
  for (;;) {
    findFrom(html, ATTR_GAP, i)
    i = ATTR_GAP.lastIndex
    if (i >= html.length) {
      return null
    }
    if (html[i] === ">") {
      return { attrs, end: i + 1 }
    }
    const name = findFrom(html, ATTR_NAME, i)![0].toLowerCase()
    i = ATTR_NAME.lastIndex
    let value = ""
    if (findFrom(html, ATTR_EQUALS, i)) {
      i = ATTR_EQUALS.lastIndex
      const quote = html[i]
      if (quote === '"' || quote === "'") {
        const endQuote = html.indexOf(quote, i + 1)
        if (endQuote === -1) {
          return null
        }
        value = html.slice(i + 1, endQuote)
        i = endQuote + 1
      } else {
        value = findFrom(html, ATTR_UNQUOTED, i)![0]
        i = ATTR_UNQUOTED.lastIndex
      }
    }
    if (!attrs.has(name)) {
      attrs.set(name, value)
    }
  }
}

const META_OPEN = /<meta[\t\n\f\r />]/gi

/** content of the first <meta name|property="key"> for each wanted key. */
function metaContents(html: string, keys: string[]) {
  const found = new Map<string, string | null>()
  let pos = 0
  while (found.size < keys.length) {
    const open = findFrom(html, META_OPEN, pos)
    const tag = open ? readAttributes(html, open.index + 5) : null
    if (!tag) {
      break
    }
    pos = tag.end
    const content = tag.attrs.get("content")
    if (content === undefined) {
      continue
    }
    for (const attr of ["name", "property"]) {
      const key = tag.attrs.get(attr)?.toLowerCase()
      if (key && keys.includes(key) && !found.has(key)) {
        found.set(key, clean(content))
      }
    }
  }
  return found
}

// The path is capped so each stored link stays small; longer ones are skipped.
const SOCIAL_LINK =
  /href=["'](https?:\/\/(?:www\.)?(?:instagram|facebook|linkedin|tiktok|x|twitter|youtube|pinterest)\.com\/[^"'#?]{1,300})["']/gi

function extractSocialLinks(html: string) {
  const links: string[] = []
  for (const match of html.matchAll(SOCIAL_LINK)) {
    if (!links.includes(match[1])) {
      links.push(match[1])
    }
    if (links.length === 8) {
      break
    }
  }
  return links
}

function boundedText(value: unknown, max: number) {
  return typeof value === "string" ? clean(value, max) : null
}

/** A schema.org address (text or PostalAddress) as one bounded line. */
function addressText(value: unknown) {
  const address = Array.isArray(value) ? value[0] : value
  if (!address || typeof address !== "object") {
    return boundedText(address, 300)
  }
  const parts = [
    "streetAddress",
    "addressLocality",
    "addressRegion",
    "postalCode",
    "addressCountry",
  ].map((key) => {
    const part = (address as Record<string, unknown>)[key]
    return part && typeof part === "object"
      ? (part as Record<string, unknown>).name
      : part
  })
  return boundedText(
    parts
      .filter((part) => typeof part === "string" || typeof part === "number")
      .join(", "),
    300
  )
}

/** Copies only bounded strings, since this goes to Claude and into the RFQ. */
function organizationSnapshot(
  org: Record<string, unknown>
): OrganizationSnapshot {
  const sameAs = Array.isArray(org.sameAs) ? org.sameAs : [org.sameAs]
  return {
    name: boundedText(org.name, 200),
    description: boundedText(org.description, 400),
    url: boundedText(org.url, 300),
    sameAs: sameAs
      .filter((value): value is string => typeof value === "string")
      .slice(0, 8)
      .map((value) => clean(value, 300))
      .filter((value): value is string => Boolean(value)),
    address: addressText(org.address),
  }
}

const SCRIPT_OPEN = /<script/gi
const SCRIPT_CLOSE = /<\/script>/gi
const LD_JSON_TYPE = /type=["']application\/ld\+json["']/i

function extractOrganization(html: string) {
  let pos = 0
  for (;;) {
    const open = findFrom(html, SCRIPT_OPEN, pos)
    const gt = open ? html.indexOf(">", SCRIPT_OPEN.lastIndex) : -1
    if (gt === -1) {
      return null
    }
    if (!LD_JSON_TYPE.test(html.slice(SCRIPT_OPEN.lastIndex, gt))) {
      pos = gt + 1
      continue
    }
    const close = findFrom(html, SCRIPT_CLOSE, gt + 1)
    if (!close) {
      return null
    }
    pos = SCRIPT_CLOSE.lastIndex
    try {
      const parsed = JSON.parse(html.slice(gt + 1, close.index).trim())
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
        return organizationSnapshot(org)
      }
    } catch {
      // ignore malformed JSON-LD
    }
  }
}

export function extractPageSnapshot(html: string, url: string): PageSnapshot {
  let withoutNoise = html
  for (const tags of [
    ["script"],
    ["style"],
    ["noscript"],
    ["svg"],
    ["nav", "footer", "header"],
  ]) {
    withoutNoise = stripElements(withoutNoise, tags)
  }

  const body = innerHtml(withoutNoise, "body", true) ?? withoutNoise
  const text = (clean(body, 20000) ?? "").slice(0, 6000)
  const meta = metaContents(html, [
    "description",
    "og:description",
    "og:site_name",
  ])

  return {
    url,
    title: clean(innerHtml(html, "title"), 200),
    description: meta.get("description") ?? meta.get("og:description") ?? null,
    site_name: meta.get("og:site_name") ?? null,
    headings: extractHeadings(withoutNoise),
    text,
    social_links: extractSocialLinks(html),
    organization: extractOrganization(html),
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
