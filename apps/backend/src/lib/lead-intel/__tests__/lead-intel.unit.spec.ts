import http from "node:http"
import { AddressInfo } from "node:net"
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
