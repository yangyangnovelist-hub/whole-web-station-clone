import { ContainerRegistrationKeys, Modules } from "@medusajs/framework/utils"
import instantQuoteFollowups from "../../jobs/instant-quote-followups"
import { RFQ_MODULE } from "../../modules/rfq"
import orderPlacedHandler from "../../subscribers/order-placed"
import { sendEmail } from "../email/send"
import { priceQuote } from "../instant-quote/engine"

jest.mock("../email/send", () => ({ sendEmail: jest.fn(async () => true) }))
jest.mock("../lead-intel/record", () => ({ recordLeadEvent: jest.fn() }))

const HOUR = 60 * 60 * 1000
const EMAIL = "sam@brand.com"
/** When the buyer requested the quote they ordered. */
const orderedAt = new Date(Date.now() - 3 * HOUR)
/** When they placed the order. */
const placedAt = new Date(Date.now() - HOUR)

function rfq(
  id: string,
  productType: string,
  createdAt: Date,
  overrides: Record<string, unknown> = {}
) {
  return {
    id,
    email: EMAIL,
    status: "QUOTED",
    source: "instant_quote",
    cart_id: `cart_${id}`,
    order_id: null,
    quoted_total: 1250,
    quote_payload: { product_type: productType },
    followup_count: 0,
    created_at: createdAt,
    ...overrides,
  }
}

function orderSetup(open: any[]) {
  const ordered = rfq("rfq_ordered", "mailer-box", orderedAt)
  const rfqService = {
    listRFQS: jest.fn(async (filters: Record<string, unknown>) =>
      "cart_id" in filters ? [ordered] : open
    ),
    markOrdered: jest.fn(async () => ({})),
    updateRFQS: jest.fn(async () => ({})),
    closeRFQ: jest.fn(async () => ({})),
  }
  const log = { warn: jest.fn(), info: jest.fn(), error: jest.fn() }
  const services: Record<string, unknown> = {
    logger: log,
    query: {
      graph: jest.fn(async () => ({ data: [{ cart_id: ordered.cart_id }] })),
    },
    [Modules.ORDER]: {
      retrieveOrder: jest.fn(async () => ({
        id: "order_1",
        display_id: 1,
        email: EMAIL,
        currency_code: "usd",
        created_at: placedAt,
        metadata: null,
        total: 1250,
        items: [
          {
            id: "item_1",
            variant_id: null,
            unit_price: 1250,
            quantity: 1,
            metadata: { packoasis_rfq_id: ordered.id },
          },
        ],
      })),
    },
    [Modules.ANALYTICS]: { track: jest.fn() },
    [RFQ_MODULE]: rfqService,
  }
  return {
    rfqService,
    log,
    run: () =>
      orderPlacedHandler({
        event: { data: { id: "order_1" } },
        container: { resolve: (key: string) => services[key] },
      } as any),
  }
}

const savedEnv = { ...process.env }
beforeEach(() => {
  jest.clearAllMocks()
  process.env.PACKOASIS_FOLLOWUP_DELAYS_HOURS = "24,72"
})
afterEach(() => {
  process.env = { ...savedEnv }
})

describe("order.placed and the buyer's other quotes", () => {
  it("stops follow-ups for open quotes of the ordered product requested before the order", async () => {
    const setup = orderSetup([
      rfq("rfq_revision", "mailer-box", new Date(orderedAt.getTime() - HOUR)),
      rfq("rfq_same_time", "mailer-box", orderedAt),
      rfq("rfq_tissue", "tissue-paper", new Date(orderedAt.getTime() - HOUR)),
      // Requested after the ordered quote (e.g. a quantity the buyer compared)
      // but before the order.
      rfq("rfq_newer", "mailer-box", new Date(orderedAt.getTime() + HOUR)),
      // Requested after the order (the event was handled late): a new quote.
      rfq("rfq_after_order", "mailer-box", new Date(placedAt.getTime() + 1)),
      rfq("rfq_done", "mailer-box", new Date(orderedAt.getTime() - HOUR), {
        followup_count: 2,
      }),
      rfq("rfq_unknown", "mailer-box", new Date(orderedAt.getTime() - HOUR), {
        quote_payload: null,
      }),
    ])

    await setup.run()

    expect(setup.rfqService.markOrdered).toHaveBeenCalledWith(
      "rfq_ordered",
      "order_1",
      1250
    )
    expect(setup.rfqService.listRFQS).toHaveBeenCalledWith(
      {
        email: EMAIL,
        source: "instant_quote",
        status: "QUOTED",
        order_id: null,
      },
      expect.objectContaining({
        select: expect.arrayContaining(["quote_payload", "created_at"]),
      })
    )
    expect(setup.rfqService.updateRFQS.mock.calls).toEqual([
      [{ id: "rfq_revision", followup_count: 2 }],
      [{ id: "rfq_same_time", followup_count: 2 }],
      [{ id: "rfq_newer", followup_count: 2 }],
    ])
    expect(setup.rfqService.closeRFQ).not.toHaveBeenCalled()
    expect(setup.log.warn).not.toHaveBeenCalled()
  })

  it("counts every configured follow-up as done", async () => {
    process.env.PACKOASIS_FOLLOWUP_DELAYS_HOURS = "24,72,168"
    const setup = orderSetup([
      rfq("rfq_revision", "mailer-box", new Date(orderedAt.getTime() - HOUR), {
        followup_count: 2,
      }),
    ])

    await setup.run()

    expect(setup.rfqService.updateRFQS).toHaveBeenCalledWith({
      id: "rfq_revision",
      followup_count: 3,
    })
  })

  it("still confirms the order when a quote cannot be updated", async () => {
    const setup = orderSetup([
      rfq("rfq_revision", "mailer-box", new Date(orderedAt.getTime() - HOUR)),
    ])
    setup.rfqService.updateRFQS.mockRejectedValue(new Error("db down"))

    await setup.run()

    expect(setup.log.warn).toHaveBeenCalledWith(
      expect.stringContaining("rfq_revision")
    )
    expect(sendEmail).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ template: "packoasis-order-confirmation" })
    )
  })
})

describe("follow-up job and the buyer's orders", () => {
  const quote = priceQuote({ product_type: "mailer-box", quantity: 1000 })
  const requestedAt = new Date(Date.now() - 30 * HOUR)

  function candidate() {
    return rfq("rfq_open", "mailer-box", requestedAt, {
      contact_name: "Sam Buyer",
      last_contacted_at: null,
      quote_payload: quote,
      enrichment_payload: null,
    })
  }

  /** An RFQ of the buyer's that was ordered at `placed`. */
  function orderedRfq(
    id: string,
    productType: string,
    createdAt: Date,
    placed: Date,
    updatedAt = placed
  ) {
    return {
      ...rfq(id, productType, createdAt),
      status: "ORDERED",
      order_id: `order_${id}`,
      updated_at: updatedAt,
      placed,
    }
  }

  function jobSetup(orderedRfqs: ReturnType<typeof orderedRfq>[]) {
    const open = candidate()
    const time = (value: Date) => value.getTime()
    const rfqService = {
      listRFQS: jest.fn(async (filters: any) => {
        if (filters.order_id?.$ne !== null) {
          return [open]
        }
        return orderedRfqs.filter(
          (row) =>
            row.email === filters.email &&
            (!filters.created_at?.$gte ||
              time(row.created_at) >= time(filters.created_at.$gte)) &&
            (!filters.created_at?.$lt ||
              time(row.created_at) < time(filters.created_at.$lt)) &&
            (!filters.updated_at?.$gte ||
              time(row.updated_at) >= time(filters.updated_at.$gte))
        )
      }),
      updateRFQS: jest.fn(async () => ({})),
      recordContact: jest.fn(async () => ({})),
      closeRFQ: jest.fn(async () => ({})),
    }
    const query = {
      graph: jest.fn(async ({ entity, filters }: any) => ({
        data:
          entity === "order"
            ? orderedRfqs
                .filter((row) => filters.id.includes(row.order_id))
                .map((row) => ({ id: row.order_id, created_at: row.placed }))
            : [{ id: filters.id, completed_at: null }],
      })),
    }
    const services: Record<string, unknown> = {
      [ContainerRegistrationKeys.LOGGER]: {
        warn: jest.fn(),
        info: jest.fn(),
        error: jest.fn(),
      },
      [ContainerRegistrationKeys.QUERY]: query,
      [RFQ_MODULE]: rfqService,
      [Modules.NOTIFICATION]: { listNotifications: jest.fn(async () => []) },
    }
    return {
      rfqService,
      query,
      run: () =>
        instantQuoteFollowups({
          resolve: (key: string) => services[key],
        } as any),
    }
  }

  function followupsSent() {
    return jest
      .mocked(sendEmail)
      .mock.calls.filter(([, input]) =>
        input.template.startsWith("packoasis-quote-followup-")
      )
  }

  beforeEach(() => {
    process.env.PACKOASIS_AI_DISABLED = "true"
    process.env.PACKOASIS_FOLLOWUPS_ENABLED = "true"
    process.env.PACKOASIS_LEAD_EVENT_RETENTION_DAYS = "0"
  })

  it("stops follow-ups when an earlier quote for the product was ordered after this one was requested", async () => {
    const setup = jobSetup([
      orderedRfq(
        "rfq_earlier",
        "mailer-box",
        new Date(requestedAt.getTime() - HOUR),
        new Date(requestedAt.getTime() + HOUR)
      ),
    ])

    await setup.run()

    expect(setup.rfqService.updateRFQS).toHaveBeenCalledWith({
      id: "rfq_open",
      followup_count: 2,
    })
    expect(setup.rfqService.closeRFQ).not.toHaveBeenCalled()
    expect(setup.rfqService.recordContact).not.toHaveBeenCalled()
    expect(followupsSent()).toEqual([])
  })

  it("stops follow-ups when a later quote for the product was ordered", async () => {
    const setup = jobSetup([
      orderedRfq(
        "rfq_later",
        "mailer-box",
        new Date(requestedAt.getTime() + HOUR),
        new Date(requestedAt.getTime() + 2 * HOUR)
      ),
    ])

    await setup.run()

    expect(setup.rfqService.updateRFQS).toHaveBeenCalledWith({
      id: "rfq_open",
      followup_count: 2,
    })
    expect(setup.query.graph).not.toHaveBeenCalledWith(
      expect.objectContaining({ entity: "order" })
    )
    expect(followupsSent()).toEqual([])
  })

  it.each([
    ["the RFQ was last updated then", undefined],
    ["the RFQ was updated after this quote", new Date(Date.now() - HOUR)],
  ])(
    "still follows up on a quote requested after the order (%s)",
    async (_, updatedAt) => {
      const placed = new Date(requestedAt.getTime() - HOUR)
      const setup = jobSetup([
        orderedRfq(
          "rfq_before",
          "mailer-box",
          new Date(requestedAt.getTime() - 2 * HOUR),
          placed,
          updatedAt ?? placed
        ),
      ])

      await setup.run()

      expect(setup.rfqService.updateRFQS).not.toHaveBeenCalled()
      expect(followupsSent()).toHaveLength(1)
      expect(setup.rfqService.recordContact).toHaveBeenCalledWith("rfq_open", {
        followup: true,
      })
    }
  )

  it("still follows up when the buyer ordered another product after this quote", async () => {
    const setup = jobSetup([
      orderedRfq(
        "rfq_tissue",
        "tissue-paper",
        new Date(requestedAt.getTime() - HOUR),
        new Date(requestedAt.getTime() + HOUR)
      ),
    ])

    await setup.run()

    expect(setup.rfqService.updateRFQS).not.toHaveBeenCalled()
    expect(followupsSent()).toHaveLength(1)
  })
})
