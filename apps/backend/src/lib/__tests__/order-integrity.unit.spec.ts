import { MedusaError, Modules } from "@medusajs/framework/utils"
import {
  completeCartWorkflow,
  createCartWorkflow,
} from "@medusajs/medusa/core-flows"
import middlewares from "../../api/middlewares"
import { GET as checkoutToken } from "../../api/packoasis/checkout-token/route"
import { POST as orderQuote } from "../../api/store/instant-quote/order/route"
import { RFQ_MODULE } from "../../modules/rfq"
import RFQModuleService, {
  findQuotedLineItem,
  sameAmount,
} from "../../modules/rfq/service"
import orderPlacedHandler from "../../subscribers/order-placed"
import { validateInstantQuoteCart } from "../../workflows/hooks/validate-instant-quote"
import { priceQuote } from "../instant-quote/engine"
import {
  signToken,
  storefrontCheckoutUrl,
  verifyToken,
} from "../packoasis-config"

jest.mock("@medusajs/medusa/core-flows", () => ({
  createCartWorkflow: jest.fn(),
  completeCartWorkflow: { hooks: { validate: jest.fn() } },
}))
jest.mock("../email/send", () => ({ sendEmail: jest.fn(async () => true) }))
jest.mock("../lead-intel/record", () => ({ recordLeadEvent: jest.fn() }))

// Captured before any mock is cleared: importing the hook file registers it.
const registeredValidateHook = (
  completeCartWorkflow.hooks.validate as unknown as jest.Mock
).mock.calls[0]?.[0]

const CART_ID = "cart_01ABCDEFGHJKMNPQRSTVWXYZ012"
const today = new Date().toISOString().slice(0, 10)
const tomorrow = new Date(Date.now() + 86_400_000).toISOString().slice(0, 10)
const yesterday = new Date(Date.now() - 86_400_000).toISOString().slice(0, 10)

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
  res.json = jest.fn((body: unknown) => {
    res.body = body
    return res
  })
  return res
}

function logger() {
  return { warn: jest.fn(), info: jest.fn(), error: jest.fn() }
}

function quoteItem(overrides: Record<string, unknown> = {}) {
  return {
    id: "item_1",
    variant_id: null,
    unit_price: 1250,
    quantity: 1,
    metadata: {
      packoasis_rfq_id: "rfq_1",
      packoasis_quote: { valid_until: tomorrow },
    },
    ...overrides,
  }
}

function quotedRfq(overrides: Record<string, unknown> = {}) {
  return {
    id: "rfq_1",
    email: "sam@brand.com",
    cart_id: CART_ID,
    status: "QUOTED",
    quoted_total: 1250,
    quote_payload: { valid_until: tomorrow, product_type: "mailer-box" },
    created_at: new Date(Date.now() - 10 * 60_000),
    ...overrides,
  }
}

beforeEach(() => {
  jest.clearAllMocks()
})

describe("signed checkout hand-off", () => {
  it("signs the storefront checkout link for its cart only", () => {
    const url = new URL(
      storefrontCheckoutUrl(CART_ID, "us", "https://shop.example.com/")
    )
    expect(url.origin + url.pathname).toBe(
      "https://shop.example.com/api/quote-checkout"
    )
    expect(url.searchParams.get("cart_id")).toBe(CART_ID)
    expect(url.searchParams.get("country")).toBe("us")
    const token = url.searchParams.get("token")
    expect(token).toBe(signToken("checkout", CART_ID))
    expect(verifyToken("checkout", CART_ID, token)).toBe(true)
    expect(verifyToken("checkout", "cart_other", token)).toBe(false)
    expect(verifyToken("resume", CART_ID, token)).toBe(false)
  })
})

describe("sameAmount / findQuotedLineItem", () => {
  it("compares to the cent, allowing for float4 readback", () => {
    expect(sameAmount(1250, 1250.004)).toBe(true)
    expect(sameAmount(1250, 1250.01)).toBe(false)
    expect(sameAmount(132092.54, 132092.55)).toBe(true)
    expect(sameAmount(132092.54, 132092.6)).toBe(false)
    expect(sameAmount(1000000.01, 1000000)).toBe(true)
    expect(sameAmount(1000000, 1000001)).toBe(false)
    expect(sameAmount(undefined, undefined)).toBe(false)
  })

  it("prefers the exact quote_payload total over quoted_total", () => {
    const item = quoteItem({ unit_price: 1300 })
    expect(
      findQuotedLineItem([item], {
        id: "rfq_1",
        quoted_total: 1250,
        quote_payload: { total: 1300 },
      })
    ).toBe(item)
    expect(
      findQuotedLineItem([item], {
        id: "rfq_1",
        quoted_total: 1300,
        quote_payload: { total: 1250 },
      })
    ).toBeUndefined()
    expect(
      findQuotedLineItem([item], { id: "rfq_1", quoted_total: 1300 })
    ).toBe(item)
    expect(findQuotedLineItem([item], { id: "rfq_1" })).toBeUndefined()
  })
})

describe("GET /packoasis/checkout-token", () => {
  async function check(query: Record<string, unknown>, carts: any[] | Error) {
    const graph = jest.fn(async () => {
      if (carts instanceof Error) {
        throw carts
      }
      return { data: carts }
    })
    const res = mockRes()
    await checkoutToken(
      { query, scope: { resolve: () => ({ graph }) } } as any,
      res
    )
    expect(res.statusCode).toBe(200)
    expect(res.headers["Cache-Control"]).toBe("no-store")
    return { body: res.body, graph }
  }

  const token = signToken("checkout", CART_ID)

  it("accepts a signed link to an open cart", async () => {
    const { body, graph } = await check({ cart_id: CART_ID, token }, [
      { id: CART_ID, completed_at: null },
    ])
    expect(body).toEqual({ valid: true })
    expect(graph).toHaveBeenCalledWith(
      expect.objectContaining({ entity: "cart", filters: { id: CART_ID } })
    )
  })

  it("rejects bad or missing tokens without looking the cart up", async () => {
    for (const query of [
      { cart_id: CART_ID },
      { cart_id: CART_ID, token: "x".repeat(32) },
      { cart_id: CART_ID, token: signToken("resume", CART_ID) },
      { cart_id: "cart_other", token },
      { cart_id: [CART_ID], token },
      { token },
    ]) {
      const { body, graph } = await check(query, [
        { id: CART_ID, completed_at: null },
      ])
      expect(body).toEqual({ valid: false })
      expect(graph).not.toHaveBeenCalled()
    }
  })

  it("rejects completed, missing and unreadable carts", async () => {
    for (const carts of [
      [{ id: CART_ID, completed_at: new Date() }],
      [],
      new Error("db down"),
    ]) {
      const { body } = await check({ cart_id: CART_ID, token }, carts)
      expect(body).toEqual({ valid: false })
    }
  })
})

describe("checkout-token rate limit", () => {
  const limiter = middlewares.routes!.find(
    (route) => route.matcher === "/packoasis/checkout-token"
  )!.middlewares![0]

  // Every request comes from the storefront, as it does in production.
  async function call(cartId: string, token: string) {
    const query = { cart_id: cartId, token }
    const req = {
      query,
      get: () => undefined,
      socket: { remoteAddress: "10.0.0.5" },
      scope: {
        resolve: () => ({
          graph: async () => ({ data: [{ id: cartId, completed_at: null }] }),
        }),
      },
    } as any
    const res = mockRes()
    let passed = false
    await limiter(req, res, () => {
      passed = true
    })
    if (passed) {
      await checkoutToken(req, res)
    }
    return res
  }

  it("does not count forged tokens against real buyers", async () => {
    for (let i = 0; i < 300; i++) {
      const res = await call(
        "cart_01AAAAAAAAAAAAAAAAAAAAAAAAAA",
        "x".repeat(32)
      )
      expect(res.statusCode).toBe(200)
      expect(res.body).toEqual({ valid: false })
    }
    const res = await call(CART_ID, signToken("checkout", CART_ID))
    expect(res.statusCode).toBe(200)
    expect(res.body).toEqual({ valid: true })
  })

  it("limits a replayed signed link without blocking other carts", async () => {
    const attackerCart = "cart_01ATTACKERATTACKERATTACKER1"
    const attackerToken = signToken("checkout", attackerCart)
    const statuses: number[] = []
    for (let i = 0; i < 40; i++) {
      statuses.push((await call(attackerCart, attackerToken)).statusCode)
    }
    expect(statuses.filter((status) => status === 429).length).toBe(10)

    const res = await call(CART_ID, signToken("checkout", CART_ID))
    expect(res.body).toEqual({ valid: true })
  })
})

describe("completeCartWorkflow validate hook", () => {
  function hookContainer(rfqs: any[]) {
    const rfqService = {
      listRFQS: jest.fn(async (filters: { cart_id: string }) =>
        rfqs.filter((rfq) => rfq.cart_id === filters.cart_id)
      ),
    }
    return {
      rfqService,
      container: {
        resolve: (key: string) => (key === RFQ_MODULE ? rfqService : null),
      } as any,
    }
  }

  async function rejection(promise: Promise<unknown>) {
    const error = await promise.then(
      () => null,
      (reason) => reason
    )
    expect(error).toBeInstanceOf(MedusaError)
    expect(error.type).toBe(MedusaError.Types.NOT_ALLOWED)
    return error.message as string
  }

  it("is registered on completeCartWorkflow.hooks.validate", async () => {
    expect(typeof registeredValidateHook).toBe("function")
    const { container, rfqService } = hookContainer([])
    await expect(
      registeredValidateHook(
        { input: { id: CART_ID }, cart: { id: CART_ID, items: [quoteItem()] } },
        { container }
      )
    ).rejects.toBeInstanceOf(MedusaError)
    expect(rfqService.listRFQS).toHaveBeenCalledWith({ cart_id: CART_ID })
  })

  it("ignores catalog-only carts and already completed carts", async () => {
    const { container, rfqService } = hookContainer([])
    await validateInstantQuoteCart(
      { id: CART_ID, items: [{ id: "i", variant_id: "variant_1" }] },
      container
    )
    await validateInstantQuoteCart(
      { id: CART_ID, completed_at: new Date(), items: [quoteItem()] },
      container
    )
    expect(rfqService.listRFQS).not.toHaveBeenCalled()
  })

  it("allows the current, unexpired quote cart at the quoted total", async () => {
    const { container } = hookContainer([
      quotedRfq({ quote_payload: { valid_until: today } }),
    ])
    await expect(
      validateInstantQuoteCart(
        {
          id: CART_ID,
          items: [quoteItem(), { id: "i2", variant_id: "variant_1" }],
        },
        container
      )
    ).resolves.toBeUndefined()
  })

  it("rejects a cart superseded by a re-priced quote", async () => {
    // The resume link re-priced the quote into a new cart.
    const { container } = hookContainer([quotedRfq({ cart_id: "cart_new" })])
    expect(
      await rejection(
        validateInstantQuoteCart(
          { id: CART_ID, items: [quoteItem()] },
          container
        )
      )
    ).toMatch(/no longer available/)
  })

  it("rejects an expired quote even if the item metadata was edited", async () => {
    const { container } = hookContainer([
      quotedRfq({ quote_payload: { valid_until: yesterday } }),
    ])
    expect(
      await rejection(
        validateInstantQuoteCart(
          {
            id: CART_ID,
            items: [
              quoteItem({
                metadata: {
                  packoasis_rfq_id: "rfq_1",
                  packoasis_quote: { valid_until: "2999-01-01" },
                },
              }),
            ],
          },
          container
        )
      )
    ).toMatch(/expired/)
  })

  it("allows a large quote whose float4 quoted_total reads back a cent off", async () => {
    // quoted_total is a Postgres real: 132092.54 is stored as 132092.546875
    // and reads back as 132092.55.
    const item = quoteItem({ unit_price: 132092.54 })
    for (const rfq of [
      quotedRfq({
        quoted_total: 132092.55,
        quote_payload: { valid_until: tomorrow, total: 132092.54 },
      }),
      quotedRfq({ quoted_total: 132092.55 }),
    ]) {
      const { container } = hookContainer([rfq])
      await expect(
        validateInstantQuoteCart({ id: CART_ID, items: [item] }, container)
      ).resolves.toBeUndefined()
    }

    const { container } = hookContainer([
      quotedRfq({
        quoted_total: 132092.55,
        quote_payload: { valid_until: tomorrow, total: 132092.54 },
      }),
    ])
    await rejection(
      validateInstantQuoteCart(
        {
          id: CART_ID,
          items: [quoteItem({ unit_price: 132092.54, quantity: 0.999 })],
        },
        container
      )
    )
  })

  it("rejects a quote item whose quantity or RFQ id was changed", async () => {
    const { container } = hookContainer([quotedRfq()])
    for (const item of [
      quoteItem({ quantity: 0.01 }),
      quoteItem({ quantity: 2 }),
      quoteItem({ metadata: { packoasis_rfq_id: "rfq_other" } }),
      quoteItem({ metadata: null }),
    ]) {
      await rejection(
        validateInstantQuoteCart({ id: CART_ID, items: [item] }, container)
      )
    }
  })
})

describe("RFQModuleService.markOrdered", () => {
  function fakeService(quotes: any[]) {
    return {
      retrieveRFQ: jest.fn(async () => ({ id: "rfq_1", status: "QUOTED" })),
      listQuotes: jest.fn(async () => quotes),
      buyerAccept: jest.fn(async () => ({ id: "rfq_1", status: "ACCEPTED" })),
      transitionStatus: jest.fn(async (id: string, status: string) => ({
        id,
        status,
      })),
      updateRFQS: jest.fn(async (data: unknown) => data),
    }
  }
  const quotes = [
    { id: "quote_1", round: 1, price: 1250, status: "PENDING" },
    { id: "quote_2", round: 2, price: 1400, status: "PENDING" },
  ]

  it("accepts the pending round priced at the ordered amount", async () => {
    const service = fakeService(quotes)
    await RFQModuleService.prototype.markOrdered.call(
      service as any,
      "rfq_1",
      "order_1",
      1250
    )
    expect(service.buyerAccept).toHaveBeenCalledWith("rfq_1", "quote_1")
    expect(service.transitionStatus).toHaveBeenCalledWith("rfq_1", "ORDERED")
    expect(service.updateRFQS).toHaveBeenCalledWith({
      id: "rfq_1",
      order_id: "order_1",
    })
  })

  it("accepts the latest pending round when no amount is given", async () => {
    const service = fakeService(quotes)
    await RFQModuleService.prototype.markOrdered.call(
      service as any,
      "rfq_1",
      "order_1"
    )
    expect(service.buyerAccept).toHaveBeenCalledWith("rfq_1", "quote_2")
  })

  it("matches a large round whose float4 price reads back a cent off", async () => {
    const service = fakeService([
      { id: "quote_1", round: 1, price: 132092.55, status: "PENDING" },
      { id: "quote_2", round: 2, price: 140000, status: "PENDING" },
    ])
    await RFQModuleService.prototype.markOrdered.call(
      service as any,
      "rfq_1",
      "order_1",
      132092.54
    )
    expect(service.buyerAccept).toHaveBeenCalledWith("rfq_1", "quote_1")
  })

  it("accepts no round when none matches the ordered amount", async () => {
    const service = fakeService(quotes)
    await RFQModuleService.prototype.markOrdered.call(
      service as any,
      "rfq_1",
      "order_1",
      999
    )
    expect(service.buyerAccept).not.toHaveBeenCalled()
    expect(service.transitionStatus).toHaveBeenCalledWith("rfq_1", "ACCEPTED")
  })
})

describe("order.placed subscriber", () => {
  function orderSetup(input: {
    items: any[]
    orderMetadata?: Record<string, unknown>
    cartId: string | null
    rfqs: any[]
    open?: any[]
  }) {
    const rfqService = {
      listRFQS: jest.fn(async (filters: Record<string, unknown>) =>
        "cart_id" in filters
          ? input.rfqs.filter((rfq) => rfq.cart_id === filters.cart_id)
          : input.open ?? []
      ),
      markOrdered: jest.fn(async () => ({})),
      closeRFQ: jest.fn(async () => ({})),
    }
    const graph = jest.fn(async () => ({
      data: input.cartId ? [{ cart_id: input.cartId }] : [],
    }))
    const log = logger()
    const services: Record<string, unknown> = {
      logger: log,
      query: { graph },
      [Modules.ORDER]: {
        retrieveOrder: jest.fn(async () => ({
          id: "order_1",
          display_id: 1,
          email: "sam@brand.com",
          currency_code: "usd",
          created_at: new Date(),
          metadata: input.orderMetadata ?? null,
          total: 1250,
          items: input.items,
        })),
      },
      [Modules.ANALYTICS]: { track: jest.fn() },
      [RFQ_MODULE]: rfqService,
    }
    return {
      rfqService,
      graph,
      log,
      run: () =>
        orderPlacedHandler({
          event: { data: { id: "order_1" } },
          container: { resolve: (key: string) => services[key] },
        } as any),
    }
  }

  it("links the RFQ quoted for the order's cart and closes the buyer's other open quotes", async () => {
    const setup = orderSetup({
      items: [quoteItem()],
      orderMetadata: { packoasis_rfq_id: "rfq_1" },
      cartId: CART_ID,
      rfqs: [quotedRfq()],
      open: [{ id: "rfq_earlier" }],
    })
    await setup.run()

    expect(setup.graph).toHaveBeenCalledWith(
      expect.objectContaining({
        entity: "order_cart",
        filters: { order_id: "order_1" },
      })
    )
    expect(setup.rfqService.markOrdered).toHaveBeenCalledTimes(1)
    expect(setup.rfqService.markOrdered).toHaveBeenCalledWith(
      "rfq_1",
      "order_1",
      1250
    )
    expect(setup.rfqService.listRFQS).toHaveBeenCalledWith(
      {
        email: "sam@brand.com",
        source: "instant_quote",
        status: "QUOTED",
        order_id: null,
      },
      expect.anything()
    )
    expect(setup.rfqService.closeRFQ).toHaveBeenCalledWith("rfq_earlier")
    expect(setup.log.warn).not.toHaveBeenCalled()
  })

  it("ignores an RFQ named only in editable metadata of a cheap order", async () => {
    // RFQ B was quoted at 30,000 for another cart; this $250 cart's metadata
    // was edited to point at it.
    const setup = orderSetup({
      items: [
        {
          id: "item_1",
          variant_id: "variant_cheap",
          unit_price: 250,
          quantity: 1,
          metadata: { packoasis_rfq_id: "rfq_B" },
        },
      ],
      orderMetadata: { packoasis_rfq_id: "rfq_B" },
      cartId: "cart_cheap",
      rfqs: [
        quotedRfq({ id: "rfq_B", cart_id: "cart_B", quoted_total: 30000 }),
      ],
    })
    await setup.run()

    expect(setup.rfqService.listRFQS).toHaveBeenCalledWith({
      cart_id: "cart_cheap",
    })
    expect(setup.rfqService.markOrdered).not.toHaveBeenCalled()
    expect(setup.rfqService.closeRFQ).not.toHaveBeenCalled()
    expect(setup.log.warn).toHaveBeenCalledWith(
      expect.stringContaining("rfq_B")
    )
  })

  it("does not link the quote cart's RFQ when the item total differs", async () => {
    const setup = orderSetup({
      items: [quoteItem({ quantity: 0.01 })],
      cartId: CART_ID,
      rfqs: [quotedRfq()],
    })
    await setup.run()
    expect(setup.rfqService.markOrdered).not.toHaveBeenCalled()
    expect(setup.log.warn).toHaveBeenCalledWith(
      expect.stringContaining("rfq_1")
    )
  })
})

describe("POST /store/instant-quote/order", () => {
  const specs = { product_type: "mailer-box", quantity: 1000 }
  const quote = priceQuote(specs)

  function orderRequest(cartRun: jest.Mock) {
    const calls: string[] = []
    const rfqService = {
      submitRFQ: jest.fn(async () => ({ id: "rfq_1" })),
      submitQuote: jest.fn(async () => {
        calls.push("submitQuote")
        return {}
      }),
      updateRFQS: jest.fn(async (data: unknown) => {
        calls.push("updateRFQS")
        return data
      }),
    }
    ;(createCartWorkflow as unknown as jest.Mock).mockReturnValue({
      run: jest.fn(async (...args: unknown[]) => {
        calls.push("createCart")
        return cartRun(...args)
      }),
    })
    const services: Record<string, unknown> = {
      logger: logger(),
      query: {
        graph: jest.fn(async () => ({
          data: [
            {
              id: "reg_1",
              currency_code: quote.currency_code,
              countries: [{ iso_2: "us" }],
            },
          ],
        })),
      },
      [RFQ_MODULE]: rfqService,
      [Modules.EVENT_BUS]: { emit: jest.fn() },
    }
    const req = {
      body: {
        specs,
        contact: { name: "Sam Buyer", email: "Sam@Brand.com" },
        country_code: "us",
      },
      publishable_key_context: { sales_channel_ids: ["sc_1"] },
      get: () => undefined,
      scope: { resolve: (key: string) => services[key] },
    } as any
    return { req, rfqService, calls }
  }

  it("records the quote round only after the cart exists", async () => {
    expect(quote.instant).toBe(true)
    const { req, rfqService, calls } = orderRequest(
      jest.fn(async () => ({ result: { id: CART_ID } }))
    )
    const res = mockRes()
    await orderQuote(req, res)

    expect(res.statusCode).toBe(201)
    expect(calls).toEqual(["createCart", "submitQuote", "updateRFQS"])
    expect(rfqService.submitRFQ).toHaveBeenCalledWith(
      expect.objectContaining({ source: "instant_quote" })
    )
    expect(rfqService.updateRFQS).toHaveBeenCalledWith({
      id: "rfq_1",
      cart_id: CART_ID,
    })
    expect(res.body.cart_id).toBe(CART_ID)
    expect(res.body.requires_review).toBe(false)
    const token = new URL(res.body.checkout_url).searchParams.get("token")
    expect(verifyToken("checkout", CART_ID, token)).toBe(true)
  })

  it("sends the RFQ to human review when the cart cannot be created", async () => {
    const { req, rfqService } = orderRequest(
      jest.fn(async () => {
        throw new Error("no tax region")
      })
    )
    const res = mockRes()
    await orderQuote(req, res)

    expect(res.statusCode).toBe(201)
    expect(rfqService.submitQuote).not.toHaveBeenCalled()
    expect(rfqService.updateRFQS).toHaveBeenCalledTimes(1)
    expect(rfqService.updateRFQS).toHaveBeenCalledWith({
      id: "rfq_1",
      source: "instant_quote_review",
    })
    expect(res.body).toEqual(
      expect.objectContaining({
        cart_id: null,
        checkout_url: null,
        requires_review: true,
      })
    )
  })
})
