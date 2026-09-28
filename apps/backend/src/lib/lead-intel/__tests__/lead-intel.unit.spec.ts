import http from "node:http"
import { AddressInfo } from "node:net"
import LeadModuleService from "../../../modules/lead/service"
import { extractPageSnapshot, resolveCompanyWebsite } from "../extract"
import {
  assertSafeUrl,
  isAllowedByRobots,
  isPublicIp,
  safeFetch,
  UnsafeUrlError,
} from "../safe-fetch"
import { heuristicLeadScore } from "../score"
import { summarizeTrail } from "../trail"

describe("SSRF guards", () => {
  it.each([
    ["8.8.8.8", true],
    ["104.16.1.1", true],
    ["10.0.0.5", false],
    ["172.20.1.1", false],
    ["192.168.1.10", false],
    ["127.0.0.1", false],
    ["169.254.169.254", false],
    ["100.64.3.2", false],
    ["0.0.0.0", false],
    ["::1", false],
    ["fd00::1", false],
    ["fe80::1", false],
    ["::ffff:10.0.0.1", false],
    ["2606:4700::6810:1", true],
    // IPv4-mapped/compatible IPv6 in hex form, as the URL parser writes them
    ["::ffff:7f00:1", false],
    ["::ffff:a9fe:a9fe", false],
    ["::ffff:a00:5", false],
    ["::7f00:1", false],
    ["0:0:0:0:0:ffff:7f00:1", false],
    ["::FFFF:127.0.0.1", false],
    ["::ffff:808:808", true],
    ["::", false],
    ["ff02::1", false],
    ["2001:db8::1", false],
    ["64:ff9b::a9fe:a9fe", false],
    ["not-an-ip", false],
  ])("isPublicIp(%s) = %s", (ip, expected) => {
    expect(isPublicIp(ip)).toBe(expected)
  })

  it("rejects non-http schemes, odd ports, credentials and internal hosts", () => {
    expect(() => assertSafeUrl("file:///etc/passwd")).toThrow(UnsafeUrlError)
    expect(() => assertSafeUrl("http://example.com:8080/")).toThrow(
      UnsafeUrlError
    )
    expect(() => assertSafeUrl("http://user:pw@example.com/")).toThrow(
      UnsafeUrlError
    )
    expect(() => assertSafeUrl("http://localhost/")).toThrow(UnsafeUrlError)
    expect(() => assertSafeUrl("http://intranet/")).toThrow(UnsafeUrlError)
    expect(() => assertSafeUrl("http://db.internal/")).toThrow(UnsafeUrlError)
    expect(() =>
      assertSafeUrl("http://169.254.169.254/latest/meta-data")
    ).toThrow(UnsafeUrlError)
    expect(assertSafeUrl("https://acme.com/about").hostname).toBe("acme.com")
  })

  it.each([
    "http://[::ffff:169.254.169.254]/latest/meta-data/",
    "http://[::ffff:127.0.0.1]/",
    "http://[::ffff:a00:5]/admin",
    "https://[0:0:0:0:0:ffff:7f00:1]/",
    "http://[::1]/",
  ])("rejects IPv6 literals for non-public addresses (%s)", (raw) => {
    expect(() => assertSafeUrl(raw)).toThrow(UnsafeUrlError)
  })

  it("refuses loopback targets and non-standard ports", async () => {
    const server = http.createServer((req, res) => {
      if (req.url === "/start") {
        res.writeHead(302, { location: "/final" })
        return res.end()
      }
      res.writeHead(200, { "content-type": "text/html" })
      res.end(
        "<html><head><title>Loopback</title></head><body>hi</body></html>"
      )
    })
    await new Promise<void>((resolve) =>
      server.listen(0, "127.0.0.1", () => resolve())
    )
    const port = (server.address() as AddressInfo).port
    try {
      await expect(safeFetch(`http://127.0.0.1:${port}/start`)).rejects.toThrow(
        UnsafeUrlError
      )

      // With the test seam allowing loopback, the redirect is followed.
      const page = await safeFetch(`http://127.0.0.1:${port}/start`, {
        allowAddress: () => true,
      }).catch((error) => error)
      // Port is non-standard, so even with the seam the URL guard refuses it.
      expect(page).toBeInstanceOf(UnsafeUrlError)
    } finally {
      server.close()
    }
  })

  it("parses robots.txt groups", () => {
    const robots =
      "User-agent: *\nDisallow: /private\n\nUser-agent: PackOasisBot\nDisallow: /\n"
    expect(isAllowedByRobots(robots, "/")).toBe(false)
    expect(isAllowedByRobots("User-agent: *\nDisallow: /private", "/")).toBe(
      true
    )
    expect(isAllowedByRobots("User-agent: *\nDisallow: /", "/")).toBe(false)
    expect(
      isAllowedByRobots("User-agent: *\nDisallow: /\nAllow: /$", "/")
    ).toBe(true)
    expect(isAllowedByRobots("", "/")).toBe(true)
    expect(isAllowedByRobots("User-agent: *\rDisallow: /\r", "/")).toBe(false)
  })

  it("parses a hostile robots.txt in linear time", () => {
    const spaces = " ".repeat(100_000)
    for (const robots of [
      `Disallow:${spaces}x\ry`,
      `User-agent: *\nDisallow:${spaces}x\u2028y`,
      `User-agent: *\nDisallow: /${"*".repeat(100_000)}\u2028`,
      `${"#".repeat(100_000)}\u2028x`,
    ]) {
      const started = Date.now()
      isAllowedByRobots(robots, "/")
      expect(Date.now() - started).toBeLessThan(1000)
    }
  })
})

describe("page extraction", () => {
  it("pulls title, description, headings, org JSON-LD and visible text", () => {
    const html = `<html><head><title>Acme Candles &amp; Co</title>
<meta name="description" content="Hand-poured soy candles.">
<meta property="og:site_name" content="Acme">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Organization","name":"Acme Candles","url":"https://acme.example"}</script>
<script>var x = "ignore me"</script></head>
<body><nav>Menu</nav><h1>Small-batch candles</h1><p>We ship to 40 states.</p>
<a href="https://www.instagram.com/acmecandles">IG</a></body></html>`
    const page = extractPageSnapshot(html, "https://acme.example/")
    expect(page.title).toBe("Acme Candles & Co")
    expect(page.description).toBe("Hand-poured soy candles.")
    expect(page.site_name).toBe("Acme")
    expect(page.headings).toEqual(["Small-batch candles"])
    expect(page.organization?.name).toBe("Acme Candles")
    expect(page.text).toContain("We ship to 40 states.")
    expect(page.text).not.toContain("ignore me")
    expect(page.text).not.toContain("Menu")
    expect(page.social_links).toEqual(["https://www.instagram.com/acmecandles"])
  })

  it("reads meta tags in any attribute order and strips tags in comments", () => {
    const html = `<HTML><HEAD><TITLE>Joe's</TITLE>
<META CONTENT="Joe's small-batch candles > the rest" NAME="Description" />
<meta property='og:site_name' content='Joe&#39;s'>
</HEAD><body class="home"><!-- <div>Old promo</div> --><h2 class="x">Our <b>story</b></h2>
<header>Top</header><p>Hand poured.</p><footer>Bottom</footer></body></HTML>`
    const page = extractPageSnapshot(html, "https://joe.example/")
    expect(page.title).toBe("Joe's")
    expect(page.description).toBe("Joe's small-batch candles > the rest")
    expect(page.site_name).toBe("Joe's")
    expect(page.headings).toEqual(["Our story"])
    expect(page.text).not.toContain("<")
    expect(page.text).toContain("Old promo")
    expect(page.text).toContain("Our story Hand poured.")
    expect(page.text).not.toContain("Top")
  })

  it("stays linear on hostile markup", () => {
    const size = 750_000
    const fill = (unit: string) =>
      unit.repeat(Math.ceil(size / unit.length)).slice(0, size)
    const normal = fill(
      `<html><head><title>Acme</title><meta name="description" content="Boxes">
<script type="application/ld+json">{"@type":"Organization","name":"Acme"}</script></head>
<body><nav>Menu</nav><h1>Custom boxes</h1><p>We ship fast &amp; cheap.</p>
<a href="https://www.instagram.com/acme">IG</a><script>var x = 1</script></body></html>`
    )
    for (const html of [
      fill("<meta "),
      fill("<"),
      fill("<h1 "),
      fill("<h1>"),
      fill("<script "),
      fill('<script type="application/ld+json">'),
      fill("<body "),
      fill("<title "),
      fill("<nav"),
      fill('<meta a="x" '),
      fill("<a <b> "),
      normal,
    ]) {
      const started = Date.now()
      const page = extractPageSnapshot(html, "https://acme.example/")
      expect(Date.now() - started).toBeLessThan(1000)
      expect(page.text.length).toBeLessThanOrEqual(6000)
    }
    const page = extractPageSnapshot(normal, "https://acme.example/")
    expect(page.title).toBe("Acme")
    expect(page.description).toBe("Boxes")
    expect(page.organization?.name).toBe("Acme")
    expect(page.headings).toHaveLength(12)
    expect(page.social_links).toEqual(["https://www.instagram.com/acme"])
  })

  it("keeps stored social links small", () => {
    const long = Array.from(
      { length: 8 },
      (_, i) =>
        `<a href="https://www.facebook.com/${i}${"p".repeat(90_000)}">f</a>`
    ).join("")
    const page = extractPageSnapshot(
      `${long}<a href='https://x.com/acme'>x</a>`,
      "https://acme.example/"
    )
    expect(page.social_links).toEqual(["https://x.com/acme"])
    const site = {
      url: page.url,
      title: page.title,
      description: page.description,
      site_name: page.site_name,
      social_links: page.social_links,
      organization: page.organization,
    }
    expect(JSON.stringify(site).length).toBeLessThan(1000)
  })

  it("bounds every JSON-LD organization field", () => {
    const org = {
      "@context": "https://schema.org",
      "@graph": [
        { "@type": "WebSite", name: "Site" },
        {
          "@type": ["Organization", "Brand"],
          name: "N".repeat(300_000),
          description: "D ".repeat(300_000),
          url: `https://acme.example/${"u".repeat(10_000)}`,
          sameAs: Array.from(
            { length: 5_000 },
            (_, i) => `https://x.com/acme${i}`
          ),
          address: {
            "@type": "PostalAddress",
            streetAddress: "1 Main St",
            addressLocality: "Austin",
            postalCode: 78701,
            addressCountry: { "@type": "Country", name: "US" },
            description: "x".repeat(100_000),
          },
          logo: "L".repeat(100_000),
        },
      ],
    }
    const page = extractPageSnapshot(
      `<script type="application/ld+json">${JSON.stringify(org)}</script>`,
      "https://acme.example/"
    )
    const snapshot = page.organization!
    expect(snapshot.name).toHaveLength(200)
    expect(snapshot.description!.length).toBeLessThanOrEqual(400)
    expect(snapshot.url).toHaveLength(300)
    expect(snapshot.sameAs).toHaveLength(8)
    expect(snapshot.sameAs[0]).toBe("https://x.com/acme0")
    expect(snapshot.address).toBe("1 Main St, Austin, 78701, US")
    expect(JSON.stringify(snapshot).length).toBeLessThan(4000)

    const plain = extractPageSnapshot(
      `<script type='application/ld+json'>{"@type":"Store","name":{"x":1},"description":"Big &#99999999; deal","sameAs":"https://www.facebook.com/acme","address":"Austin, TX"}</script>`,
      "https://acme.example/"
    )
    expect(plain.organization).toEqual({
      name: null,
      description: "Big &#99999999; deal",
      url: null,
      sameAs: ["https://www.facebook.com/acme"],
      address: "Austin, TX",
    })
  })

  it("resolves the company website from input or business email", () => {
    expect(resolveCompanyWebsite({ website: "acme.com/shop" })).toBe(
      "https://acme.com/"
    )
    expect(resolveCompanyWebsite({ email: "jo@acme-candles.com" })).toBe(
      "https://acme-candles.com/"
    )
    expect(resolveCompanyWebsite({ email: "jo@gmail.com" })).toBeNull()
    expect(
      resolveCompanyWebsite({ website: "not a url", email: "x@qq.com" })
    ).toBeNull()
  })
})

describe("browsing trail and scoring", () => {
  const events = [
    {
      type: "page_view",
      path: "/",
      title: "Home",
      referrer: "https://google.com/",
      created_at: "2026-09-26T10:00:00Z",
    },
    {
      type: "page_view",
      path: "/custom-mailers.html",
      title: "Mailers",
      product_type: "mailer-box",
      created_at: "2026-09-26T10:02:00Z",
    },
    {
      type: "quote_open",
      product_type: "mailer-box",
      created_at: "2026-09-26T10:03:00Z",
    },
    {
      type: "quote_priced",
      product_type: "mailer-box",
      created_at: "2026-09-26T10:04:00Z",
    },
    {
      type: "page_view",
      path: "/custom-mailers.html",
      title: "Mailers",
      product_type: "mailer-box",
      created_at: "2026-09-26T10:05:00Z",
    },
    {
      type: "page_view",
      path: "/rigid.html",
      product_type: "rigid-box",
      payload: { utm: { utm_source: "google" } },
      created_at: "2026-09-26T12:00:00Z",
    },
  ]

  it("summarizes pages, interest and active time", () => {
    const trail = summarizeTrail(events)
    expect(trail.pages_viewed).toBe(4)
    expect(trail.top_pages[0]).toEqual({
      path: "/custom-mailers.html",
      title: "Mailers",
      views: 2,
    })
    expect(trail.product_interest[0]).toEqual({
      product_type: "mailer-box",
      signals: 8,
    })
    expect(trail.quote_interactions).toBe(2)
    expect(trail.active_minutes).toBe(5)
    expect(trail.entry_referrer).toBe("https://google.com/")
    expect(trail.utm).toEqual({ utm_source: "google" })
  })

  describe("listVisitorTrail", () => {
    type Stored = { id: string; created_at: Date }
    // Orders on created_at only, stable on ties, like the real query.
    const serviceFor = (stored: Stored[]) => {
      const listLeadEvents = jest.fn(
        async (
          _filters: unknown,
          config: { take: number; order: { created_at: "ASC" | "DESC" } }
        ) => {
          const sign = config.order.created_at === "ASC" ? 1 : -1
          return [...stored]
            .sort(
              (a, b) => sign * (a.created_at.getTime() - b.created_at.getTime())
            )
            .slice(0, config.take)
        }
      )
      const service = { listLeadEvents } as unknown as LeadModuleService
      const ids = async (limit: number) => {
        const events = await LeadModuleService.prototype.listVisitorTrail.call(
          service,
          "visitor_1",
          limit
        )
        return events.map((event) => event.id)
      }
      return { listLeadEvents, ids }
    }

    it("lists the newest events oldest-first and keeps the first visit", async () => {
      const stored = Array.from({ length: 6 }, (_, i) => ({
        id: `levt_${i}`,
        created_at: new Date(Date.UTC(2026, 8, 20 + i)),
      }))
      const { listLeadEvents, ids } = serviceFor(stored)

      expect(await ids(3)).toEqual(["levt_0", "levt_3", "levt_4", "levt_5"])
      expect(await ids(6)).toEqual(stored.map((event) => event.id))
      listLeadEvents.mockClear()
      expect(await ids(10)).toEqual(stored.map((event) => event.id))
      expect(listLeadEvents).toHaveBeenCalledTimes(1)
    })

    it("never repeats an event when the oldest ones share a created_at", async () => {
      // The first two events came in one widget flush.
      const stored = Array.from({ length: 200 }, (_, i) => ({
        id: `levt_${i}`,
        created_at: new Date(Date.UTC(2026, 8, 1, 0, Math.max(i, 1))),
      }))
      const { ids } = serviceFor(stored)

      for (const limit of [200, 199, 150]) {
        const trail = await ids(limit)
        expect(new Set(trail).size).toBe(trail.length)
        expect(trail.slice(-3)).toEqual(["levt_197", "levt_198", "levt_199"])
      }
      expect(await ids(200)).toHaveLength(200)
      expect(await ids(199)).toHaveLength(199)
      expect(await ids(150)).toHaveLength(151)
    })
  })

  it("scores business buyers with intent higher", () => {
    const strong = heuristicLeadScore({
      email: "buyer@brand.com",
      company: "Brand",
      phone: "123",
      quote_total: 12000,
      rush: true,
      trail: summarizeTrail(events),
    })
    const weak = heuristicLeadScore({
      email: "someone@gmail.com",
      quote_total: 300,
    })
    expect(strong.score).toBeGreaterThan(weak.score)
    expect(strong.grade).toBe("A")
    expect(weak.grade).toBe("D")
    expect(strong.reasons).toContain("Business email domain")
  })
})
