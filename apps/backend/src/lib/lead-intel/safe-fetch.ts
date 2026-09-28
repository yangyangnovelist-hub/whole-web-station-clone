import dns from "node:dns"
import http from "node:http"
import https from "node:https"
import net from "node:net"

/**
 * Fetches a public web page for lead enrichment without letting user-supplied
 * domains reach internal infrastructure (SSRF): only http(s) on 80/443, every
 * resolved address is checked at connect time (no DNS-rebinding window),
 * redirects are re-validated, and time/size are capped.
 */

export class UnsafeUrlError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "UnsafeUrlError"
  }
}

const BLOCKED_V4: [string, number][] = [
  ["0.0.0.0", 8],
  ["10.0.0.0", 8],
  ["100.64.0.0", 10],
  ["127.0.0.0", 8],
  ["169.254.0.0", 16],
  ["172.16.0.0", 12],
  ["192.0.0.0", 24],
  ["192.0.2.0", 24],
  ["192.88.99.0", 24],
  ["192.168.0.0", 16],
  ["198.18.0.0", 15],
  ["198.51.100.0", 24],
  ["203.0.113.0", 24],
  ["224.0.0.0", 4],
  ["240.0.0.0", 4],
]

// ::/96 unspecified, loopback and IPv4-compatible, fc00::/7 unique local,
// fe80::/10 link local, ff00::/8 multicast, 2001:db8::/32 docs,
// 64:ff9b::/32 NAT64 (well-known and local-use prefixes)
const BLOCKED_V6: [string, number][] = [
  ["::", 96],
  ["fc00::", 7],
  ["fe80::", 10],
  ["ff00::", 8],
  ["2001:db8::", 32],
  ["64:ff9b::", 32],
]

// BlockList parses every textual IPv6 form and checks IPv4-mapped addresses
// (::ffff:a.b.c.d, ::ffff:7f00:1, ...) against the IPv4 rules.
const BLOCKLIST = new net.BlockList()
for (const [base, bits] of BLOCKED_V4) {
  BLOCKLIST.addSubnet(base, bits, "ipv4")
}
for (const [base, bits] of BLOCKED_V6) {
  BLOCKLIST.addSubnet(base, bits, "ipv6")
}

export function isPublicIp(address: string): boolean {
  const family = net.isIP(address)
  if (family === 4) {
    return !BLOCKLIST.check(address, "ipv4")
  }
  if (family === 6) {
    return !BLOCKLIST.check(address, "ipv6")
  }
  return false
}

export function assertSafeUrl(
  raw: string,
  allowAddress: (ip: string) => boolean = isPublicIp
): URL {
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    throw new UnsafeUrlError(`Invalid URL: ${raw}`)
  }

  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new UnsafeUrlError(`Unsupported protocol ${url.protocol}`)
  }
  if (url.username || url.password) {
    throw new UnsafeUrlError("Credentials in URL are not allowed")
  }
  if (url.port && url.port !== "80" && url.port !== "443") {
    throw new UnsafeUrlError(`Port ${url.port} is not allowed`)
  }

  const host = url.hostname.replace(/^\[|\]$/g, "")
  if (net.isIP(host)) {
    if (!allowAddress(host)) {
      throw new UnsafeUrlError(`Address ${host} is not public`)
    }
  } else if (
    !host.includes(".") ||
    /\.(local|localhost|internal|lan|home|corp|intranet)$/i.test(host) ||
    /^localhost$/i.test(host)
  ) {
    throw new UnsafeUrlError(`Host ${host} is not a public domain`)
  }

  return url
}

type LookupCallback = (
  err: NodeJS.ErrnoException | null,
  address: string | dns.LookupAddress[],
  family?: number
) => void

/** dns.lookup wrapper used by the socket itself, so the checked IP is the dialed IP. */
const safeLookup = (allowAddress: (ip: string) => boolean) =>
  function lookup(
    hostname: string,
    options: dns.LookupOptions,
    callback: LookupCallback
  ) {
    dns.lookup(hostname, { ...options, all: true }, (err, addresses) => {
      if (err) {
        return callback(err, [])
      }
      const list = addresses as dns.LookupAddress[]
      const blocked = list.find((entry) => !allowAddress(entry.address))
      if (!list.length || blocked) {
        return callback(
          new UnsafeUrlError(
            `Host ${hostname} resolves to a non-public address`
          ) as NodeJS.ErrnoException,
          []
        )
      }
      if (options.all) {
        return callback(null, list)
      }
      callback(null, list[0].address, list[0].family)
    })
  }

export type SafeFetchResult = {
  url: string
  status: number
  contentType: string
  body: string
}

export type SafeFetchOptions = {
  timeoutMs?: number
  maxBytes?: number
  maxRedirects?: number
  accept?: string
  userAgent?: string
  /** Test seam; production always uses isPublicIp. */
  allowAddress?: (ip: string) => boolean
}

const DEFAULT_UA =
  "PackOasisBot/1.0 (+https://packoasis.com/llms.txt; lead enrichment for quote requests)"

function requestOnce(
  url: URL,
  options: Required<SafeFetchOptions>
): Promise<{
  status: number
  headers: http.IncomingHttpHeaders
  body: string
}> {
  return new Promise((resolve, reject) => {
    const client = url.protocol === "https:" ? https : http
    const req = client.request(
      url,
      {
        method: "GET",
        lookup: safeLookup(
          options.allowAddress
        ) as unknown as net.LookupFunction,
        headers: {
          "user-agent": options.userAgent,
          accept: options.accept,
          "accept-language": "en-US,en;q=0.8",
        },
      },
      (res) => {
        const chunks: Buffer[] = []
        let size = 0
        res.on("data", (chunk: Buffer) => {
          size += chunk.length
          if (size > options.maxBytes) {
            chunks.push(
              chunk.subarray(
                0,
                Math.max(0, options.maxBytes - (size - chunk.length))
              )
            )
            res.destroy()
            return
          }
          chunks.push(chunk)
        })
        const finish = () =>
          resolve({
            status: res.statusCode ?? 0,
            headers: res.headers,
            body: Buffer.concat(chunks).toString("utf8"),
          })
        res.on("end", finish)
        res.on("close", finish)
        res.on("error", reject)
      }
    )
    const timer = setTimeout(() => {
      req.destroy(new Error(`Timed out after ${options.timeoutMs}ms`))
    }, options.timeoutMs)
    req.on("error", reject)
    req.on("close", () => clearTimeout(timer))
    req.end()
  })
}

export async function safeFetch(
  rawUrl: string,
  opts: SafeFetchOptions = {}
): Promise<SafeFetchResult> {
  const options: Required<SafeFetchOptions> = {
    timeoutMs: opts.timeoutMs ?? 6000,
    maxBytes: opts.maxBytes ?? 750_000,
    maxRedirects: opts.maxRedirects ?? 3,
    accept: opts.accept ?? "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
    userAgent: opts.userAgent ?? DEFAULT_UA,
    allowAddress: opts.allowAddress ?? isPublicIp,
  }

  let url = assertSafeUrl(rawUrl, options.allowAddress)
  for (let hop = 0; hop <= options.maxRedirects; hop++) {
    const res = await requestOnce(url, options)
    if (res.status >= 300 && res.status < 400 && res.headers.location) {
      url = assertSafeUrl(
        new URL(res.headers.location, url).toString(),
        options.allowAddress
      )
      continue
    }

    return {
      url: url.toString(),
      status: res.status,
      contentType: String(res.headers["content-type"] ?? ""),
      body: res.body,
    }
  }

  throw new UnsafeUrlError(`Too many redirects for ${rawUrl}`)
}

/** Minimal robots.txt check for "*" and our bot on the path we want. */
export function isAllowedByRobots(
  robotsTxt: string,
  path: string,
  agent = "packoasisbot"
) {
  const groups: {
    agents: string[]
    rules: { allow: boolean; path: string }[]
  }[] = []
  let current: (typeof groups)[number] | null = null
  let lastWasAgent = false

  // Split on every robots.txt EOL; [\s\S]* with no trailing $ can't backtrack,
  // so a hostile file (100 KB of spaces before a \u2028) stays linear.
  for (const rawLine of robotsTxt.split(/\r\n|\r|\n/)) {
    const line = rawLine.replace(/#[\s\S]*/, "").trim()
    const match = line.match(/^([A-Za-z-]+)\s*:([\s\S]*)/)
    if (!match) {
      continue
    }
    const field = match[1].toLowerCase()
    const value = match[2].trim()
    if (field === "user-agent") {
      if (!current || !lastWasAgent) {
        current = { agents: [], rules: [] }
        groups.push(current)
      }
      current.agents.push(value.toLowerCase())
      lastWasAgent = true
    } else if ((field === "allow" || field === "disallow") && current) {
      current.rules.push({ allow: field === "allow", path: value })
      lastWasAgent = false
    } else {
      lastWasAgent = false
    }
  }

  const group =
    groups.find((g) => g.agents.some((a) => a !== "*" && agent.includes(a))) ??
    groups.find((g) => g.agents.includes("*"))
  if (!group) {
    return true
  }

  let best: { allow: boolean; length: number } | null = null
  for (const rule of group.rules) {
    if (!rule.path) {
      continue
    }
    const prefix = rule.path.replace(/\*[\s\S]*/, "").replace(/\$$/, "")
    // Longest match wins; on a tie the less restrictive (Allow) rule wins.
    if (
      path.startsWith(prefix) &&
      (!best ||
        prefix.length > best.length ||
        (prefix.length === best.length && rule.allow))
    ) {
      best = { allow: rule.allow, length: prefix.length }
    }
  }
  return best ? best.allow : true
}
