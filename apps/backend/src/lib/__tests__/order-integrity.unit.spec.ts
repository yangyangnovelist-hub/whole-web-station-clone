import path from "path"
import { MedusaError, Modules } from "@medusajs/framework/utils"
import {
  completeCartWorkflow,
  createCartWorkflow,
} from "@medusajs/medusa/core-flows"
import middlewares from "../../api/middlewares"
import { GET as checkoutToken } from "../../api/packoasis/checkout-token/route"
import { POST as resumeCart } from "../../api/packoasis/resume-cart/route"
import { GET as legacyResume } from "../../api/packoasis/resume/route"
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
  packoasisConfig,
  resumeQuoteUrl,
  signToken,
  storefrontCheckoutUrl,
  verifyCheckoutLink,
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
// The widget's po_qn cookie nonce: 32 random bytes, base64url.
const HANDOFF = "Hn0nce_".repeat(6) + "x"
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
  res.redirect = jest.fn((status: number, location: string) => {
    res.statusCode = status
    res.location = location
    return res
  })
  return res
}

/** The query /api/quote-checkout forwards for a widget checkout link. */
function checkoutLink(cartId = CART_ID, handoff = HANDOFF) {
  const url = new URL(
    storefrontCheckoutUrl(cartId, "us", handoff, "https://shop.example.com")
  )
  return {
    cart_id: url.searchParams.get("cart_id")!,
    exp: url.searchParams.get("exp")!,
    token: url.searchParams.get("token")!,
    handoff,
  }
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
  it("signs the checkout link for its cart, the widget's nonce and 30 minutes", () => {
    const now = Math.floor(Date.now() / 1000)
    const url = new URL(
      storefrontCheckoutUrl(CART_ID, "us", HANDOFF, "https://shop.example.com/")
    )
    expect(url.origin + url.pathname).toBe(
      "https://shop.example.com/api/quote-checkout"
    )
    expect(url.searchParams.get("cart_id")).toBe(CART_ID)
    expect(url.searchParams.get("country")).toBe("us")
    // The nonce stays in the buyer's cookie, never in the link.
    expect(url.href).not.toContain(HANDOFF)
    const exp = Number(url.searchParams.get("exp"))
    expect(exp).toBeGreaterThanOrEqual(now + 1800)
    expect(exp).toBeLessThanOrEqual(now + 1801)
    expect(url.searchParams.get("token")).toBe(
      signToken("checkout", `${CART_ID}:${HANDOFF}:${exp}`)
    )
    expect(verifyCheckoutLink(checkoutLink())).toBe(CART_ID)
  })

  it("refuses the link in another browser, for another cart or with a changed expiry", () => {
    const link = checkoutLink()
    for (const input of [
      { ...link, handoff: "0therBrowser".repeat(4) },
      { ...link, handoff: undefined },
      { ...link, handoff: "" },
      { ...link, handoff: "short" },
      { ...link, cart_id: "cart_01OTHERCARTOTHERCARTOTHER01" },
      { ...link, exp: String(Number(link.exp) + 3600) },
      { ...link, exp: Number(link.exp) },
      { ...link, token: signToken("checkout", CART_ID) },
      { ...link, token: signToken("resume", CART_ID) },
      { ...link, token: undefined },
    ]) {
      expect(verifyCheckoutLink(input)).toBeNull()
    }
  })

  it("refuses an expired link", () => {
    const link = checkoutLink()
    const clock = jest
      .spyOn(Date, "now")
      .mockReturnValue(Number(link.exp) * 1000 + 1)
    try {
      expect(verifyCheckoutLink(link)).toBeNull()
    } finally {
      clock.mockRestore()
    }
    const exp = String(Math.floor(Date.now() / 1000) - 1)
    expect(
      verifyCheckoutLink({
        cart_id: CART_ID,
        handoff: HANDOFF,
        exp,
        token: signToken("checkout", `${CART_ID}:${HANDOFF}:${exp}`),
      })
    ).toBeNull()
  })

  it("sends emailed links through the storefront's quote-resume route", () => {
    const url = new URL(resumeQuoteUrl("01RFQ"))
    expect(url.origin).toBe(packoasisConfig.storefrontUrl())
    expect(url.pathname).toBe("/api/quote-resume")
    expect(url.searchParams.get("rfq")).toBe("01RFQ")
    expect(url.searchParams.get("token")).toBe(signToken("resume", "01RFQ"))
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
  async function check(
    query: Record<string, unknown>,
    carts: any[] | Error,
    rfqs: any[] = [quotedRfq()]
  ) {
    const graph = jest.fn(async () => {
      if (carts instanceof Error) {
        throw carts
      }
      return { data: carts }
    })
    const rfqService = {
      listRFQS: jest.fn(async (filters: { cart_id: string }) =>
        rfqs.filter((rfq) => rfq.cart_id === filters.cart_id)
      ),
    }
    const services: Record<string, unknown> = {
      query: { graph },
      [RFQ_MODULE]: rfqService,
    }
    const res = mockRes()
    await checkoutToken(
      { query, scope: { resolve: (key: string) => services[key] } } as any,
      res
    )
    expect(res.statusCode).toBe(200)
    expect(res.headers["Cache-Control"]).toBe("no-store")
    return { body: res.body, graph, rfqService }
  }

  const openCart = [{ id: CART_ID, completed_at: null }]

  it("accepts a link signed for this browser's nonce to its quote's open cart", async () => {
    const { body, graph, rfqService } = await check(checkoutLink(), openCart)
    expect(body).toEqual({ valid: true })
    expect(graph).toHaveBeenCalledWith(
      expect.objectContaining({ entity: "cart", filters: { id: CART_ID } })
    )
    expect(rfqService.listRFQS).toHaveBeenCalledWith({ cart_id: CART_ID })
  })

  it("rejects forged, rebound and expired links without looking the cart up", async () => {
    const link = checkoutLink()
    const past = String(Math.floor(Date.now() / 1000) - 60)
    for (const query of [
      { cart_id: CART_ID, token: link.token, exp: link.exp },
      { ...link, handoff: "AttackerNonce_".repeat(3) },
      { ...link, token: "x".repeat(32) },
      { ...link, token: signToken("checkout", CART_ID) },
      { ...link, cart_id: "cart_01OTHERCARTOTHERCARTOTHER01" },
      { ...link, cart_id: [CART_ID] },
      { ...link, exp: past },
      {
        ...link,
        exp: past,
        token: signToken("checkout", `${CART_ID}:${HANDOFF}:${past}`),
      },
      { token: link.token, exp: link.exp, handoff: HANDOFF },
    ]) {
      const { body, graph } = await check(query, openCart)
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
      const { body } = await check(checkoutLink(), carts)
      expect(body).toEqual({ valid: false })
    }
  })

  it("rejects carts that are superseded or whose quote was ordered or closed", async () => {
    for (const rfqs of [
      [],
      [quotedRfq({ cart_id: "cart_new" })],
      [quotedRfq({ status: "ORDERED", order_id: "order_1" })],
      [quotedRfq({ order_id: "order_1" })],
      [quotedRfq({ status: "CLOSED" })],
    ]) {
      const { body } = await check(checkoutLink(), openCart, rfqs)
      expect(body).toEqual({ valid: false })
    }
  })

  it("sends an expired quote to its resume link to be re-priced", async () => {
    const { body } = await check(checkoutLink(), openCart, [
      quotedRfq({ quote_payload: { valid_until: yesterday } }),
    ])
    expect(body).toEqual({ valid: false, resume_url: resumeQuoteUrl("rfq_1") })
  })
})

describe("checkout-token rate limit", () => {
  const limiter = middlewares.routes!.find(
    (route) => route.matcher === "/packoasis/checkout-token"
  )!.middlewares![0]

  // Every request comes from the storefront, as it does in production.
  async function call(query: Record<string, unknown>) {
    const cartId = query.cart_id as string
    const services: Record<string, unknown> = {
      query: {
        graph: async () => ({ data: [{ id: cartId, completed_at: null }] }),
      },
      [RFQ_MODULE]: { listRFQS: async () => [quotedRfq({ cart_id: cartId })] },
    }
    const req = {
      query,
      get: () => undefined,
      socket: { remoteAddress: "10.0.0.5" },
      scope: { resolve: (key: string) => services[key] },
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
    const forged = {
      ...checkoutLink("cart_01AAAAAAAAAAAAAAAAAAAAAAAAAA"),
      token: "x".repeat(32),
    }
    for (let i = 0; i < 300; i++) {
      const res = await call(forged)
      expect(res.statusCode).toBe(200)
      expect(res.body).toEqual({ valid: false })
    }
    const res = await call(checkoutLink())
    expect(res.statusCode).toBe(200)
    expect(res.body).toEqual({ valid: true })
  })

  it("limits a replayed signed link without blocking other carts", async () => {
    const attackerLink = checkoutLink("cart_01ATTACKERATTACKERATTACKER1")
    const statuses: number[] = []
    for (let i = 0; i < 40; i++) {
      statuses.push((await call(attackerLink)).statusCode)
    }
    expect(statuses.filter((status) => status === 429).length).toBe(10)

    const res = await call(checkoutLink())
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
    // A resume link moved the quote to a fresh cart.
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

  it("rejects a closed quote", async () => {
    const { container } = hookContainer([quotedRfq({ status: "CLOSED" })])
    expect(
      await rejection(
        validateInstantQuoteCart(
          { id: CART_ID, items: [quoteItem()] },
          container
        )
      )
    ).toMatch(/closed/)
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
      updateRFQS: jest.fn(async () => ({})),
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

  it("links the RFQ quoted for the order's cart and only stops reminders for earlier quotes of the same product", async () => {
    const hourAgo = new Date(Date.now() - 60 * 60_000)
    const setup = orderSetup({
      items: [quoteItem()],
      orderMetadata: { packoasis_rfq_id: "rfq_1" },
      cartId: CART_ID,
      rfqs: [quotedRfq()],
      open: [
        {
          id: "rfq_earlier",
          quote_payload: { product_type: "mailer-box" },
          created_at: hourAgo,
        },
        {
          id: "rfq_other_product",
          quote_payload: { product_type: "label" },
          created_at: hourAgo,
        },
        {
          id: "rfq_later",
          quote_payload: { product_type: "mailer-box" },
          created_at: new Date(),
        },
      ],
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
    expect(setup.rfqService.closeRFQ).not.toHaveBeenCalled()
    expect(setup.rfqService.updateRFQS).toHaveBeenCalledTimes(1)
    expect(setup.rfqService.updateRFQS).toHaveBeenCalledWith({
      id: "rfq_earlier",
      followup_count: 2,
    })
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

  function orderRequest(
    cartRun: jest.Mock,
    body: Record<string, unknown> = { handoff: HANDOFF }
  ) {
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
        ...body,
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
  })

  it("creates the quote cart for the buyer at the quoted total", async () => {
    const cartRun = jest.fn(async () => ({ result: { id: CART_ID } }))
    const { req } = orderRequest(cartRun)
    await orderQuote(req, mockRes())

    const input = (cartRun.mock.calls[0] as any)[0].input
    expect(input).toMatchObject({
      region_id: "reg_1",
      sales_channel_id: "sc_1",
      email: "sam@brand.com",
      currency_code: quote.currency_code,
      metadata: { packoasis_rfq_id: "rfq_1" },
    })
    expect(input.items).toEqual([
      expect.objectContaining({
        quantity: 1,
        unit_price: quote.total,
        requires_shipping: false,
        metadata: expect.objectContaining({ packoasis_rfq_id: "rfq_1" }),
      }),
    ])
  })

  it("returns a checkout link bound to the widget's handoff nonce", async () => {
    const { req } = orderRequest(
      jest.fn(async () => ({ result: { id: CART_ID } }))
    )
    const res = mockRes()
    await orderQuote(req, res)

    const url = new URL(res.body.checkout_url)
    expect(url.origin).toBe(packoasisConfig.storefrontUrl())
    expect(url.pathname).toBe("/api/quote-checkout")
    expect(url.href).not.toContain(HANDOFF)
    const link = {
      cart_id: url.searchParams.get("cart_id"),
      exp: url.searchParams.get("exp"),
      token: url.searchParams.get("token"),
    }
    expect(link.cart_id).toBe(CART_ID)
    expect(url.searchParams.get("country")).toBe("us")
    expect(Number(link.exp)).toBeGreaterThan(Date.now() / 1000 + 1790)
    expect(verifyCheckoutLink({ ...link, handoff: HANDOFF })).toBe(CART_ID)
    expect(
      verifyCheckoutLink({ ...link, handoff: "SomeoneElse_".repeat(4) })
    ).toBeNull()
  })

  it("sends a widget without a handoff nonce to the resume link, never the bare cart", async () => {
    const { req } = orderRequest(
      jest.fn(async () => ({ result: { id: CART_ID } })),
      {}
    )
    const res = mockRes()
    await orderQuote(req, res)
    expect(res.statusCode).toBe(201)
    expect(res.body.checkout_url).toBe(resumeQuoteUrl("rfq_1"))
  })

  it("rejects a malformed handoff nonce", async () => {
    for (const handoff of ["short", "x".repeat(129), "has space".repeat(3)]) {
      const { req, rfqService } = orderRequest(jest.fn(), { handoff })
      const res = mockRes()
      await orderQuote(req, res)
      expect(res.statusCode).toBe(400)
      expect(rfqService.submitRFQ).not.toHaveBeenCalled()
    }
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

describe("POST /packoasis/resume-cart", () => {
  const quote = priceQuote({ product_type: "mailer-box", quantity: 1000 })
  const NEW_CART = "cart_01NEWCARTNEWCARTNEWCARTNEW"

  function resumeRequest(
    rfq: any,
    options: { body?: Record<string, unknown>; cart?: any } = {}
  ) {
    let carts = 0
    const cartRun = jest.fn(async () => ({
      result: { id: `${NEW_CART}${++carts}` },
    }))
    ;(createCartWorkflow as unknown as jest.Mock).mockReturnValue({
      run: cartRun,
    })
    const rfqService = {
      retrieveRFQ: jest.fn(async (id: string) => {
        if (!rfq || rfq.id !== id) {
          throw new Error("not found")
        }
        return rfq
      }),
      submitQuote: jest.fn(async () => ({})),
      updateRFQS: jest.fn(async (data: unknown) => data),
    }
    const cart =
      "cart" in options
        ? options.cart
        : {
            id: CART_ID,
            region_id: "reg_1",
            sales_channel_id: "sc_1",
            completed_at: null,
          }
    const graph = jest.fn(async () => ({ data: cart ? [cart] : [] }))
    const services: Record<string, unknown> = {
      logger: logger(),
      query: { graph },
      [RFQ_MODULE]: rfqService,
    }
    const req = {
      body: options.body ?? {
        rfq: "rfq_1",
        token: signToken("resume", "rfq_1"),
      },
      scope: { resolve: (key: string) => services[key] },
    } as any
    return { req, rfqService, cartRun, graph }
  }

  function currentQuote(overrides: Record<string, unknown> = {}) {
    return quotedRfq({
      quote_payload: quote,
      quoted_total: quote.total,
      country_code: "ca",
      ...overrides,
    })
  }

  it("mints a fresh cart at the locked price on every click, without an email", async () => {
    expect(quote.valid_until >= today).toBe(true)
    const { req, rfqService, cartRun, graph } = resumeRequest(currentQuote())

    for (const n of [1, 2]) {
      const res = mockRes()
      await resumeCart(req, res)
      expect(res.statusCode).toBe(200)
      expect(res.body).toEqual({
        cart_id: `${NEW_CART}${n}`,
        country_code: "ca",
      })
      expect(rfqService.updateRFQS).toHaveBeenLastCalledWith({
        id: "rfq_1",
        cart_id: `${NEW_CART}${n}`,
      })
    }
    expect(graph).toHaveBeenCalledWith(
      expect.objectContaining({ entity: "cart", filters: { id: CART_ID } })
    )
    const input = (cartRun.mock.calls[0] as any)[0].input
    expect(input).toMatchObject({
      region_id: "reg_1",
      sales_channel_id: "sc_1",
      currency_code: quote.currency_code,
      metadata: {
        packoasis_rfq_id: "rfq_1",
        packoasis_source: "instant_quote",
      },
    })
    expect(input.email).toBeUndefined()
    expect(input.items).toEqual([
      expect.objectContaining({
        quantity: 1,
        unit_price: quote.total,
        requires_shipping: false,
        metadata: expect.objectContaining({ packoasis_rfq_id: "rfq_1" }),
      }),
    ])
    expect(rfqService.submitQuote).not.toHaveBeenCalled()
  })

  it("re-prices an expired quote into a new round and cart", async () => {
    const stale = { ...quote, total: 1, valid_until: yesterday }
    const { req, rfqService, cartRun } = resumeRequest(
      currentQuote({ quote_payload: stale, quoted_total: 1 })
    )
    const res = mockRes()
    await resumeCart(req, res)

    const fresh = priceQuote(quote.specs)
    expect(res.body.cart_id).toBe(`${NEW_CART}1`)
    expect((cartRun.mock.calls[0] as any)[0].input.items[0].unit_price).toBe(
      fresh.total
    )
    expect(rfqService.submitQuote).toHaveBeenCalledWith(
      expect.objectContaining({ rfq_id: "rfq_1", price: fresh.total })
    )
    expect(rfqService.updateRFQS).toHaveBeenCalledWith({
      id: "rfq_1",
      cart_id: `${NEW_CART}1`,
      quote_payload: fresh,
      quoted_total: fresh.total,
    })
  })

  it("refuses bad links, ordered or closed quotes and completed carts", async () => {
    const cases: [any, Record<string, unknown>, number][] = [
      [currentQuote(), { body: { rfq: "rfq_1", token: "x".repeat(32) } }, 403],
      [
        currentQuote(),
        { body: { rfq: "rfq_1", token: signToken("checkout", "rfq_1") } },
        403,
      ],
      [currentQuote(), { body: {} }, 403],
      [
        currentQuote(),
        { body: { rfq: "rfq_2", token: signToken("resume", "rfq_2") } },
        404,
      ],
      [currentQuote({ status: "ORDERED", order_id: "order_1" }), {}, 409],
      [currentQuote({ order_id: "order_1" }), {}, 409],
      [currentQuote({ status: "CLOSED" }), {}, 409],
      [currentQuote({ cart_id: null }), {}, 409],
      [currentQuote({ quote_payload: null }), {}, 409],
      [currentQuote(), { cart: null }, 409],
      [
        currentQuote(),
        { cart: { id: CART_ID, completed_at: new Date() } },
        409,
      ],
    ]
    for (const [rfq, options, status] of cases) {
      const { req, rfqService, cartRun } = resumeRequest(rfq, options)
      const res = mockRes()
      await resumeCart(req, res)
      expect(res.statusCode).toBe(status)
      expect(res.body.cart_id).toBeUndefined()
      expect(cartRun).not.toHaveBeenCalled()
      expect(rfqService.updateRFQS).not.toHaveBeenCalled()
    }
  })

  it("leaves the quote on its cart when the new cart cannot be created", async () => {
    const { req, rfqService, cartRun } = resumeRequest(currentQuote())
    cartRun.mockRejectedValueOnce(new Error("no tax region"))
    const res = mockRes()
    await resumeCart(req, res)
    expect(res.statusCode).toBe(500)
    expect(rfqService.updateRFQS).not.toHaveBeenCalled()
  })

  it("caps the carts one signed link can mint without counting forged ones", async () => {
    const limiter = middlewares.routes!.find(
      (route) => route.matcher === "/packoasis/resume-cart"
    )!.middlewares![0]
    async function call(body: Record<string, unknown>) {
      const { req } = resumeRequest(currentQuote({ id: body.rfq }), { body })
      Object.assign(req, {
        get: () => undefined,
        socket: { remoteAddress: "10.0.0.5" },
      })
      const res = mockRes()
      let passed = false
      await limiter(req, res, () => {
        passed = true
      })
      if (passed) {
        await resumeCart(req, res)
      }
      return res.statusCode
    }

    const forged = { rfq: "rfq_victim", token: "x".repeat(32) }
    for (let i = 0; i < 50; i++) {
      expect(await call(forged)).toBe(403)
    }
    const signed = { rfq: "rfq_limit", token: signToken("resume", "rfq_limit") }
    const statuses: number[] = []
    for (let i = 0; i < 15; i++) {
      statuses.push(await call(signed))
    }
    expect(statuses.filter((status) => status === 200).length).toBe(10)
    expect(statuses.filter((status) => status === 429).length).toBe(5)
    expect(
      await call({
        rfq: "rfq_victim",
        token: signToken("resume", "rfq_victim"),
      })
    ).toBe(200)
  })
})

describe("GET /packoasis/resume", () => {
  it("forwards links from earlier emails to the storefront's quote-resume route", async () => {
    const token = signToken("resume", "rfq_1")
    const res = mockRes()
    await legacyResume({ query: { rfq: "rfq_1", token } } as any, res)
    expect(res.statusCode).toBe(302)
    expect(res.location).toBe(resumeQuoteUrl("rfq_1"))
  })
})

describe("storefront /api/quote-resume", () => {
  // The storefront route lives outside this package, so it is loaded at run
  // time with the storefront's own Next.js.
  const storefront = path.resolve(__dirname, "../../../../storefront")
  const route = require(path.join(storefront, "src/app/api/quote-resume/route"))
  const { NextRequest } = require(
    require.resolve("next/server", { paths: [storefront] })
  )
  const SHOP = "https://shop.example.com"
  const NEW_CART = "cart_01NEWCARTNEWCARTNEWCARTNEW"
  let fetchSpy: jest.SpyInstance | undefined

  afterEach(() => {
    fetchSpy?.mockRestore()
  })

  /**
   * A quote whose widget cart (CART_ID) the buyer is checking out with. The
   * storefront's server-to-server calls reach the backend route handlers.
   */
  function setup() {
    const quote = priceQuote({ product_type: "mailer-box", quantity: 1000 })
    const rfq: any = quotedRfq({
      quote_payload: quote,
      quoted_total: quote.total,
      country_code: "ca",
    })
    const rfqService = {
      retrieveRFQ: jest.fn(async () => rfq),
      listRFQS: jest.fn(async (filters: { cart_id: string }) =>
        rfq.cart_id === filters.cart_id ? [rfq] : []
      ),
      submitQuote: jest.fn(async () => ({})),
      updateRFQS: jest.fn(async (data: unknown) => Object.assign(rfq, data)),
    }
    ;(createCartWorkflow as unknown as jest.Mock).mockReturnValue({
      run: jest.fn(async () => ({ result: { id: NEW_CART } })),
    })
    const graph = jest.fn(async ({ filters }: any) => ({
      data: [
        {
          id: filters.id,
          region_id: "reg_1",
          sales_channel_id: "sc_1",
          completed_at: null,
        },
      ],
    }))
    const services: Record<string, unknown> = {
      logger: logger(),
      query: { graph },
      [RFQ_MODULE]: rfqService,
    }
    const scope = { resolve: (key: string) => services[key] }
    fetchSpy = jest
      .spyOn(global, "fetch")
      .mockImplementation(async (input: any, init?: RequestInit) => {
        expect(new URL(String(input)).pathname).toBe("/packoasis/resume-cart")
        const res = mockRes()
        await resumeCart(
          { body: JSON.parse(String(init?.body)), scope } as any,
          res
        )
        return new Response(JSON.stringify(res.body), {
          status: res.statusCode,
          headers: { "content-type": "application/json" },
        })
      })
    async function widgetCartValid() {
      const res = mockRes()
      await checkoutToken({ query: checkoutLink(), scope } as any, res)
      return res.body.valid
    }
    return { rfq, fetch: fetchSpy, widgetCartValid }
  }

  function emailLink(token = signToken("resume", "rfq_1")) {
    const url = new URL(resumeQuoteUrl("rfq_1"))
    url.searchParams.set("token", token)
    return new NextRequest(`${SHOP}/api/quote-resume${url.search}`)
  }

  function confirm(site: string, token = signToken("resume", "rfq_1")) {
    return new NextRequest(`${SHOP}/api/quote-resume`, {
      method: "POST",
      body: new URLSearchParams({ rfq: "rfq_1", token }),
      headers: { "sec-fetch-site": site },
    })
  }

  it("leaves the widget cart valid when the email link is only opened", async () => {
    const { rfq, fetch, widgetCartValid } = setup()
    expect(await widgetCartValid()).toBe(true)

    // The buyer on another device, a mail scanner, a link preview.
    for (let i = 0; i < 3; i++) {
      const res = await route.GET(emailLink())
      expect(res.status).toBe(200)
      expect(res.headers.get("cache-control")).toBe("no-store")
      expect(res.cookies.get("_medusa_cart_id")).toBeUndefined()
      const html = await res.text()
      expect(html).toContain('<form method="post" action="/api/quote-resume">')
      expect(html).toContain('name="rfq" value="rfq_1"')
      expect(html).toContain(
        `name="token" value="${signToken("resume", "rfq_1")}"`
      )
    }

    expect(fetch).not.toHaveBeenCalled()
    expect(rfq.cart_id).toBe(CART_ID)
    expect(await widgetCartValid()).toBe(true)
  })

  it("mints a fresh cart for this browser once the buyer confirms", async () => {
    const { rfq, fetch, widgetCartValid } = setup()
    const res = await route.POST(confirm("same-origin"))

    expect(res.status).toBe(303)
    expect(res.headers.get("location")).toBe(`${SHOP}/ca/checkout?step=address`)
    expect(res.cookies.get("_medusa_cart_id")).toMatchObject({
      value: NEW_CART,
      httpOnly: true,
      sameSite: "lax",
      path: "/",
    })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(rfq.cart_id).toBe(NEW_CART)
    // Deliberately continuing here supersedes the widget cart.
    expect(await widgetCartValid()).toBe(false)
  })

  it("shows cross-site posts the confirmation page and sends bad links to contact us", async () => {
    const { rfq, fetch } = setup()

    const crossSite = await route.POST(confirm("cross-site"))
    expect(crossSite.status).toBe(303)
    const target = new URL(crossSite.headers.get("location"))
    expect(target.origin + target.pathname).toBe(`${SHOP}/api/quote-resume`)
    expect(target.searchParams.get("rfq")).toBe("rfq_1")
    expect(target.searchParams.get("token")).toBe(signToken("resume", "rfq_1"))
    expect(crossSite.cookies.get("_medusa_cart_id")).toBeUndefined()
    expect(fetch).not.toHaveBeenCalled()

    const contactUs = `${SHOP}/contact-us.html`
    for (const res of [
      await route.GET(new NextRequest(`${SHOP}/api/quote-resume?rfq=rfq_1`)),
      await route.GET(emailLink("bad token!")),
      await route.POST(confirm("same-origin", "x".repeat(32))),
    ]) {
      expect(res.status).toBe(303)
      expect(res.headers.get("location")).toBe(contactUs)
      expect(res.cookies.get("_medusa_cart_id")).toBeUndefined()
    }
    // Only the forged token reached the backend, which refused it.
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(rfq.cart_id).toBe(CART_ID)
  })
})
