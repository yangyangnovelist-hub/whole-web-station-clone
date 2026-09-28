import { Modules } from "@medusajs/framework/utils"
import { RFQ_MODULE } from "../../modules/rfq"
import orderPlacedHandler from "../../subscribers/order-placed"
import { sendEmail } from "../email/send"

jest.mock("../email/send", () => ({ sendEmail: jest.fn(async () => true) }))
jest.mock("../lead-intel/record", () => ({ recordLeadEvent: jest.fn() }))

const HOUR = 60 * 60 * 1000
const EMAIL = "sam@brand.com"
const orderedAt = new Date(Date.now() - HOUR)

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
        created_at: new Date(),
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
  it("stops follow-ups only for earlier open quotes of the ordered product", async () => {
    const setup = orderSetup([
      rfq("rfq_revision", "mailer-box", new Date(orderedAt.getTime() - HOUR)),
      rfq("rfq_same_time", "mailer-box", orderedAt),
      rfq("rfq_tissue", "tissue-paper", new Date(orderedAt.getTime() - HOUR)),
      rfq("rfq_newer", "mailer-box", new Date(orderedAt.getTime() + HOUR)),
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
