import { ContainerRegistrationKeys, Modules } from "@medusajs/framework/utils"
import instantQuoteFollowups from "../../../jobs/instant-quote-followups"
import { RFQ_MODULE } from "../../../modules/rfq"
import { priceQuote } from "../../instant-quote/engine"
import { sendEmail } from "../send"

const HOUR = 60 * 60 * 1000
const email = { subject: "Hello", html: "<p>Hello</p>", text: "Hello" }

type Row = { id: string; status: "pending" | "success" | "failure" }

function makeNotifications(existing: Record<string, Row[]> = {}) {
  return {
    listNotifications: jest.fn(
      async (filters: { idempotency_key: string }) =>
        existing[filters.idempotency_key] ?? []
    ),
    softDeleteNotifications: jest.fn(async () => undefined),
    createNotifications: jest.fn(async (_data: any) => ({ id: "noti_new" })),
  }
}

function makeLogger() {
  return { info: jest.fn(), warn: jest.fn(), error: jest.fn() }
}

function makeContainer(services: Record<string, unknown>) {
  return { resolve: (key: string) => services[key] } as any
}

const savedEnv = { ...process.env }
beforeEach(() => {
  delete process.env.PACKOASIS_EMAIL_PREVIEW_DIR
})
afterEach(() => {
  process.env = { ...savedEnv }
})

describe("sendEmail idempotency", () => {
  it("sends without a key and skips the lookup", async () => {
    const notifications = makeNotifications()
    const sent = await sendEmail(
      makeContainer({
        [ContainerRegistrationKeys.LOGGER]: makeLogger(),
        [Modules.NOTIFICATION]: notifications,
      }),
      { to: "a@b.com", email, template: "t" }
    )
    expect(sent).toBe(true)
    expect(notifications.listNotifications).not.toHaveBeenCalled()
    expect(notifications.createNotifications).toHaveBeenCalledWith(
      expect.objectContaining({ to: "a@b.com", idempotency_key: null })
    )
  })

  it.each(["success", "pending"] as const)(
    "counts a %s key as sent without sending again",
    async (status) => {
      const notifications = makeNotifications({
        "followup:r1:1:a@b.com": [{ id: "noti_1", status }],
      })
      const sent = await sendEmail(
        makeContainer({
          [ContainerRegistrationKeys.LOGGER]: makeLogger(),
          [Modules.NOTIFICATION]: notifications,
        }),
        { to: "a@b.com", email, template: "t", idempotencyKey: "followup:r1:1" }
      )
      expect(sent).toBe(true)
      expect(notifications.listNotifications).toHaveBeenCalledWith(
        { idempotency_key: "followup:r1:1:a@b.com" },
        expect.anything()
      )
      expect(notifications.createNotifications).not.toHaveBeenCalled()
      expect(notifications.softDeleteNotifications).not.toHaveBeenCalled()
    }
  )

  it("frees a failed key before retrying it", async () => {
    const notifications = makeNotifications({
      "followup:r1:1:a@b.com": [{ id: "noti_failed", status: "failure" }],
    })
    const sent = await sendEmail(
      makeContainer({
        [ContainerRegistrationKeys.LOGGER]: makeLogger(),
        [Modules.NOTIFICATION]: notifications,
      }),
      { to: "a@b.com", email, template: "t", idempotencyKey: "followup:r1:1" }
    )
    expect(sent).toBe(true)
    expect(notifications.softDeleteNotifications).toHaveBeenCalledWith([
      "noti_failed",
    ])
    expect(notifications.createNotifications).toHaveBeenCalledWith(
      expect.objectContaining({ idempotency_key: "followup:r1:1:a@b.com" })
    )
    expect(
      notifications.softDeleteNotifications.mock.invocationCallOrder[0]
    ).toBeLessThan(
      notifications.createNotifications.mock.invocationCallOrder[0]
    )
  })

  it("keys each recipient and reports a failed one", async () => {
    const notifications = makeNotifications()
    notifications.createNotifications.mockImplementation(async (data: any) => {
      if (data.to === "b@c.com") {
        throw new Error("provider down")
      }
      return { id: "noti_new" }
    })
    const logger = makeLogger()
    const sent = await sendEmail(
      makeContainer({
        [ContainerRegistrationKeys.LOGGER]: logger,
        [Modules.NOTIFICATION]: notifications,
      }),
      {
        to: ["a@b.com", "b@c.com"],
        email,
        template: "t",
        idempotencyKey: "sales-order:o1",
      }
    )
    expect(sent).toBe(false)
    expect(
      notifications.createNotifications.mock.calls.map(
        ([data]: any[]) => data.idempotency_key
      )
    ).toEqual(["sales-order:o1:a@b.com", "sales-order:o1:b@c.com"])
    expect(logger.warn).toHaveBeenCalledWith(
      expect.stringContaining("provider down")
    )
  })

  it("never throws when the lookup fails", async () => {
    const notifications = makeNotifications()
    notifications.listNotifications.mockRejectedValue(new Error("db down"))
    const sent = await sendEmail(
      makeContainer({
        [ContainerRegistrationKeys.LOGGER]: makeLogger(),
        [Modules.NOTIFICATION]: notifications,
      }),
      { to: "a@b.com", email, template: "t", idempotencyKey: "k" }
    )
    expect(sent).toBe(false)
    expect(notifications.createNotifications).not.toHaveBeenCalled()
  })
})

describe("instant quote follow-up job", () => {
  const quote = priceQuote({ product_type: "mailer-box", quantity: 1000 })

  beforeEach(() => {
    process.env.PACKOASIS_AI_DISABLED = "true"
    process.env.PACKOASIS_FOLLOWUPS_ENABLED = "true"
    process.env.PACKOASIS_FOLLOWUP_DELAYS_HOURS = "24,72"
    process.env.PACKOASIS_LEAD_EVENT_RETENTION_DAYS = "0"
  })

  function makeRfq(id: string, overrides: Record<string, unknown> = {}) {
    return {
      id,
      email: `${id}@brand.com`,
      contact_name: "Sam Buyer",
      company: "Brand",
      created_at: new Date(Date.now() - 30 * HOUR),
      followup_count: 0,
      last_contacted_at: null,
      quote_payload: quote,
      cart_id: `cart_${id}`,
      enrichment_payload: null,
      ...overrides,
    }
  }

  function setup(
    candidates: Record<string, any>[],
    ordered: Record<string, any>[] = [],
    notifications = makeNotifications()
  ) {
    const rfqService = {
      listRFQS: jest.fn(async (filters: any, config: any) => {
        if (filters.order_id?.$ne === null) {
          return ordered
            .filter(
              (row) =>
                row.email === filters.email &&
                row.created_at >= filters.created_at.$gte
            )
            .slice(0, config.take)
        }
        const skip = config.skip ?? 0
        return candidates.slice(skip, skip + config.take)
      }),
      recordContact: jest.fn(async () => undefined),
      closeRFQ: jest.fn(async () => undefined),
    }
    const query = {
      graph: jest.fn(async ({ filters }: any) => ({
        data: [{ id: filters.id, completed_at: null }],
      })),
    }
    const container = makeContainer({
      [ContainerRegistrationKeys.LOGGER]: makeLogger(),
      [ContainerRegistrationKeys.QUERY]: query,
      [RFQ_MODULE]: rfqService,
      [Modules.NOTIFICATION]: notifications,
    })
    return { container, rfqService, notifications }
  }

  it("queries only rows that can be due, page by page", async () => {
    const recent = new Date().toISOString()
    const notDue = Array.from({ length: 200 }, (_, index) =>
      makeRfq(`busy${index}`, { last_contacted_at: recent })
    )
    const { container, rfqService, notifications } = setup([
      ...notDue,
      makeRfq("due"),
    ])

    await instantQuoteFollowups(container)

    const [filters, config] = rfqService.listRFQS.mock.calls[0]
    expect(filters).toMatchObject({
      source: "instant_quote",
      status: "QUOTED",
      order_id: null,
      cart_id: { $ne: null },
      followup_count: { $lt: 2 },
    })
    const windowStart = Date.now() - (72 + 7 * 24) * HOUR
    expect(
      Math.abs(filters.created_at.$gte.getTime() - windowStart)
    ).toBeLessThan(60_000)
    expect(config).toMatchObject({ skip: 0, take: 200 })
    expect(rfqService.listRFQS.mock.calls[1][1]).toMatchObject({ skip: 200 })
    expect(notifications.createNotifications).toHaveBeenCalledTimes(1)
    expect(notifications.createNotifications).toHaveBeenCalledWith(
      expect.objectContaining({
        to: "due@brand.com",
        idempotency_key: "followup:due:1:due@brand.com",
      })
    )
    expect(rfqService.recordContact).toHaveBeenCalledWith("due", {
      followup: true,
    })
  })

  it("backs off after a failed send without counting it", async () => {
    const notifications = makeNotifications()
    notifications.createNotifications.mockRejectedValue(new Error("429"))
    const { container, rfqService } = setup([makeRfq("r1")], [], notifications)

    await instantQuoteFollowups(container)

    expect(rfqService.recordContact).toHaveBeenCalledTimes(1)
    expect(rfqService.recordContact).toHaveBeenCalledWith("r1")
  })

  it("counts a delivered follow-up without resending", async () => {
    const notifications = makeNotifications({
      "followup:r1:1:r1@brand.com": [{ id: "noti_1", status: "success" }],
    })
    const { container, rfqService } = setup([makeRfq("r1")], [], notifications)

    await instantQuoteFollowups(container)

    expect(notifications.createNotifications).not.toHaveBeenCalled()
    expect(rfqService.recordContact).toHaveBeenCalledWith("r1", {
      followup: true,
    })
  })

  it("skips RFQs without a cart", async () => {
    const { container, rfqService, notifications } = setup([
      makeRfq("r1", { cart_id: null }),
    ])

    await instantQuoteFollowups(container)

    expect(notifications.createNotifications).not.toHaveBeenCalled()
    expect(rfqService.recordContact).not.toHaveBeenCalled()
  })

  it("closes the quote when the buyer ordered a later quote", async () => {
    const rfq = makeRfq("r1")
    const { container, rfqService, notifications } = setup(
      [rfq],
      [
        {
          id: "r2",
          email: rfq.email,
          created_at: new Date(rfq.created_at.getTime() + HOUR),
        },
      ]
    )

    await instantQuoteFollowups(container)

    expect(rfqService.closeRFQ).toHaveBeenCalledWith("r1")
    expect(notifications.createNotifications).not.toHaveBeenCalled()
    expect(rfqService.recordContact).not.toHaveBeenCalled()
  })

  it("still follows up when only an earlier quote was ordered", async () => {
    const rfq = makeRfq("r1")
    const { container, rfqService, notifications } = setup(
      [rfq],
      [
        {
          id: "r0",
          email: rfq.email,
          created_at: new Date(rfq.created_at.getTime() - HOUR),
        },
      ]
    )

    await instantQuoteFollowups(container)

    expect(rfqService.closeRFQ).not.toHaveBeenCalled()
    expect(notifications.createNotifications).toHaveBeenCalledTimes(1)
  })
})
