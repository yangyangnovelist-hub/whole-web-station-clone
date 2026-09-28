import { Modules } from "@medusajs/framework/utils"
import { GET, POST } from "../../api/packoasis/unsubscribe/route"
import { POST as createRfq } from "../../api/store/rfqs/route"
import { PROJECT_DRAFT_MODULE } from "../../modules/project-draft"
import { RFQ_MODULE } from "../../modules/rfq"
import {
  claimAcknowledgement,
  releaseAcknowledgement,
} from "../../subscribers/instant-quote-lead"
import { greetingName, renderRequestReceivedEmail } from "../email/templates"
import { clientIp, rateLimit } from "../http"
import { signToken } from "../packoasis-config"

function mockReq(input: {
  headers?: Record<string, string>
  query?: Record<string, unknown>
  body?: unknown
  remoteAddress?: string
  services?: Record<string, unknown>
}) {
  const headers = Object.fromEntries(
    Object.entries(input.headers ?? {}).map(([key, value]) => [
      key.toLowerCase(),
      value,
    ])
  )
  return {
    get: (name: string) => headers[name.toLowerCase()],
    query: input.query ?? {},
    body: input.body,
    socket: { remoteAddress: input.remoteAddress ?? "10.0.0.1" },
    scope: { resolve: (key: string) => input.services?.[key] },
  } as any
}

function mockRes() {
  const res: any = { statusCode: 200, headers: {}, body: undefined }
  res.status = jest.fn((code: number) => {
    res.statusCode = code
    return res
  })
  res.setHeader = jest.fn((name: string, value: unknown) => {
    res.headers[name] = value
    return res
  })
  res.type = jest.fn((type: string) => {
    res.contentType = type
    return res
  })
  res.send = jest.fn((body: unknown) => {
    res.body = body
    return res
  })
  res.json = jest.fn((body: unknown) => {
    res.body = body
    return res
  })
  return res
}

describe("clientIp", () => {
  const original = process.env.TRUST_CF_CONNECTING_IP
  afterEach(() => {
    if (original === undefined) {
      delete process.env.TRUST_CF_CONNECTING_IP
    } else {
      process.env.TRUST_CF_CONNECTING_IP = original
    }
  })

  it("ignores CF-Connecting-IP unless explicitly trusted", () => {
    delete process.env.TRUST_CF_CONNECTING_IP
    const req = mockReq({
      headers: {
        "cf-connecting-ip": "1.1.1.1",
        "x-forwarded-for": "6.6.6.6, 203.0.113.9",
      },
    })
    expect(clientIp(req)).toBe("203.0.113.9")

    process.env.TRUST_CF_CONNECTING_IP = "true"
    expect(clientIp(req)).toBe("1.1.1.1")
  })

  it("falls back to the socket address", () => {
    delete process.env.TRUST_CF_CONNECTING_IP
    expect(
      clientIp(
        mockReq({
          headers: { "cf-connecting-ip": "1.1.1.1" },
          remoteAddress: "198.51.100.7",
        })
      )
    ).toBe("198.51.100.7")
  })
})

describe("rateLimit", () => {
  function hit(
    limiter: ReturnType<typeof rateLimit>,
    headers = {},
    ip = "10.0.0.1"
  ) {
    let status = 0
    const res: any = {
      setHeader: () => res,
      status: (code: number) => {
        status = code
        return res
      },
      json: () => res,
    }
    limiter(mockReq({ headers, remoteAddress: ip }), res, () => {
      status = 200
    })
    return status
  }

  it("cannot be bypassed by rotating a spoofed CF-Connecting-IP", () => {
    delete process.env.TRUST_CF_CONNECTING_IP
    const limiter = rateLimit({ key: "spoof", limit: 3, windowMs: 60_000 })
    const statuses = Array.from({ length: 5 }, (_, index) =>
      hit(limiter, { "cf-connecting-ip": `10.0.0.${index}` }, "192.0.2.1")
    )
    expect(statuses).toEqual([200, 200, 200, 429, 429])
  })

  it("stays bounded by evicting the oldest buckets", () => {
    const limiter = rateLimit({ key: "evict", limit: 1, windowMs: 60_000 })
    expect(hit(limiter, {}, "first")).toBe(200)
    expect(hit(limiter, {}, "first")).toBe(429)

    for (let index = 0; index < 50_000; index++) {
      hit(limiter, {}, `ip-${index}`)
    }

    // The newest bucket is still tracked, the oldest one was evicted.
    expect(hit(limiter, {}, "ip-49999")).toBe(429)
    expect(hit(limiter, {}, "first")).toBe(200)
  })
})

describe("unsubscribe", () => {
  const rfqId = "rfq_01ABC"
  const token = signToken("unsubscribe", rfqId)

  function services() {
    const rfqService = { updateRFQS: jest.fn().mockResolvedValue({}) }
    return { rfqService, services: { [RFQ_MODULE]: rfqService } }
  }

  it("GET only renders a confirmation form, it never opts out", async () => {
    const { rfqService, services: scope } = services()
    const res = mockRes()
    await GET(mockReq({ query: { rfq: rfqId, token }, services: scope }), res)

    expect(res.statusCode).toBe(200)
    expect(res.body).toContain('<form method="post"')
    expect(res.body).toContain(
      `action="/packoasis/unsubscribe?rfq=${rfqId}&amp;token=${token}"`
    )
    expect(rfqService.updateRFQS).not.toHaveBeenCalled()
  })

  it("GET rejects an invalid token", async () => {
    const { rfqService, services: scope } = services()
    const res = mockRes()
    await GET(
      mockReq({ query: { rfq: rfqId, token: "nope" }, services: scope }),
      res
    )
    expect(res.statusCode).toBe(400)
    expect(rfqService.updateRFQS).not.toHaveBeenCalled()
  })

  it("POST opts out (RFC 8058 one-click gets JSON)", async () => {
    const { rfqService, services: scope } = services()
    const res = mockRes()
    await POST(
      mockReq({
        query: { rfq: rfqId, token },
        body: { "List-Unsubscribe": "One-Click" },
        services: scope,
      }),
      res
    )
    expect(rfqService.updateRFQS).toHaveBeenCalledWith({
      id: rfqId,
      contact_opt_out: true,
    })
    expect(res.statusCode).toBe(200)
    expect(res.body).toEqual({ unsubscribed: true })
  })

  it("POST from the browser form renders HTML", async () => {
    const { rfqService, services: scope } = services()
    const res = mockRes()
    await POST(
      mockReq({
        headers: { accept: "text/html,application/xhtml+xml,*/*;q=0.8" },
        query: { rfq: rfqId, token },
        services: scope,
      }),
      res
    )
    expect(rfqService.updateRFQS).toHaveBeenCalledTimes(1)
    expect(res.contentType).toBe("html")
    expect(res.body).toContain("You will not receive more reminders")
  })

  it("POST rejects an invalid token", async () => {
    const { rfqService, services: scope } = services()
    const res = mockRes()
    await POST(mockReq({ query: { rfq: rfqId }, services: scope }), res)
    expect(res.statusCode).toBe(400)
    expect(res.body).toEqual({ unsubscribed: false })
    expect(rfqService.updateRFQS).not.toHaveBeenCalled()
  })
})

describe("acknowledgement email", () => {
  it("only greets with a plain first name", () => {
    expect(greetingName("Sam Lee")).toBe("Sam")
    expect(greetingName("  José García")).toBe("José")
    expect(greetingName("Jean-Luc Picard")).toBe("Jean-Luc")
    expect(greetingName("O’Brien")).toBe("O’Brien")
    expect(greetingName("https://evil.example/login now")).toBe("there")
    expect(greetingName("<b>Hi</b>")).toBe("there")
    expect(greetingName("A".repeat(41))).toBe("there")
    expect(greetingName("")).toBe("there")
    expect(greetingName(null)).toBe("there")
  })

  it("references the request only by its server-side ref", () => {
    const email = renderRequestReceivedEmail({
      firstName: "Sam",
      rfqId: "01ABCDEFGHJKMNPQRSTVWXYZ012",
      quote: null,
    })
    expect(email.subject).toContain("XYZ012")
    expect(email.html).toContain("Thanks for your request.")
    expect(email.text).toContain("Thanks for your request (VWXYZ012).")
  })
})

describe("POST /store/rfqs validation", () => {
  function setup() {
    const rfqService = {
      submitRFQ: jest.fn(async (data: any) => ({ id: "rfq_1", ...data })),
    }
    const eventBus = { emit: jest.fn() }
    const services = {
      [RFQ_MODULE]: rfqService,
      [PROJECT_DRAFT_MODULE]: {},
      [Modules.EVENT_BUS]: eventBus,
    }
    return { rfqService, eventBus, services }
  }

  const valid = {
    contact_name: "Sam Lee",
    email: " Sam@Brand.COM ",
    title: "Mailer box RFQ",
  }

  it("rejects oversized free text and item lists", async () => {
    for (const body of [
      { ...valid, title: "x".repeat(201) },
      { ...valid, contact_name: "x".repeat(121) },
      { ...valid, notes: "x".repeat(5001) },
      { ...valid, items: Array.from({ length: 51 }, () => ({ title: "Box" })) },
      { ...valid, items: [{ title: "Box", url: "x".repeat(2001) }] },
    ]) {
      const { rfqService, services } = setup()
      const res = mockRes()
      await createRfq(mockReq({ body, services }), res)
      expect(res.statusCode).toBe(400)
      expect(res.body.code).toBe("VALIDATION_ERROR")
      expect(rfqService.submitRFQ).not.toHaveBeenCalled()
    }
  })

  it("stores a normalized email", async () => {
    const { rfqService, eventBus, services } = setup()
    const res = mockRes()
    await createRfq(mockReq({ body: valid, services }), res)
    expect(res.statusCode).toBe(201)
    expect(rfqService.submitRFQ.mock.calls[0][0].email).toBe("sam@brand.com")
    expect(eventBus.emit).toHaveBeenCalledTimes(1)
  })
})

describe("claimAcknowledgement", () => {
  const DAY = 24 * 60 * 60 * 1000

  it("sends at most one acknowledgement per address per 24h", async () => {
    const rfqService = { listRFQS: jest.fn().mockResolvedValue([]) }
    const now = Date.UTC(2026, 0, 1)

    await expect(
      claimAcknowledgement(rfqService, { id: "r1", email: "Buyer@X.com" }, now)
    ).resolves.toBe(true)
    expect(rfqService.listRFQS).toHaveBeenCalledWith(
      {
        id: { $ne: "r1" },
        email: ["Buyer@X.com", "buyer@x.com"],
        last_contacted_at: { $gte: new Date(now - DAY) },
      },
      { select: ["id"], take: 1 }
    )

    // A concurrent or later RFQ for the same address is held back in memory.
    const later = { id: "r2", email: "buyer@x.com" }
    await expect(
      claimAcknowledgement(rfqService, later, now + 1000)
    ).resolves.toBe(false)
    expect(rfqService.listRFQS).toHaveBeenCalledTimes(1)

    // After the window the database decides again.
    const nextDay = { id: "r3", email: "buyer@x.com" }
    await expect(
      claimAcknowledgement(rfqService, nextDay, now + DAY + 1)
    ).resolves.toBe(true)
    expect(rfqService.listRFQS).toHaveBeenCalledTimes(2)
  })

  it("skips when another RFQ for the address was emailed recently", async () => {
    const rfqService = {
      listRFQS: jest
        .fn()
        .mockResolvedValueOnce([{ id: "older" }])
        .mockResolvedValueOnce([]),
    }
    const email = { id: "r1", email: "repeat@x.com" }
    await expect(claimAcknowledgement(rfqService, email)).resolves.toBe(false)
    // The skip does not hold a claim, so the DB is consulted next time.
    await expect(claimAcknowledgement(rfqService, email)).resolves.toBe(true)
  })

  it("fails closed on lookup errors and frees the claim on failed sends", async () => {
    const failing = { listRFQS: jest.fn().mockRejectedValue(new Error("db")) }
    await expect(
      claimAcknowledgement(failing, { id: "r1", email: "down@x.com" })
    ).resolves.toBe(false)

    const rfqService = { listRFQS: jest.fn().mockResolvedValue([]) }
    const rfq = { id: "r2", email: "bounce@x.com" }
    await expect(claimAcknowledgement(rfqService, rfq)).resolves.toBe(true)
    releaseAcknowledgement("Bounce@X.com")
    await expect(claimAcknowledgement(rfqService, rfq)).resolves.toBe(true)
  })
})
