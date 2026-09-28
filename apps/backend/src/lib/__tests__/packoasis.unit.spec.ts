import { Script } from "vm"
import { GET as widgetRoute } from "../../api/packoasis/widget.js/route"
import {
  renderFollowupEmail,
  renderQuoteEmail,
  renderSalesLeadEmail,
} from "../email/templates"
import { priceQuote } from "../instant-quote/engine"
import { signToken, verifyToken } from "../packoasis-config"
import { packoasisWidget } from "../widget/widget-client"

describe("signed links", () => {
  it("verifies tokens per purpose and id", () => {
    const token = signToken("resume", "rfq_1")
    expect(verifyToken("resume", "rfq_1", token)).toBe(true)
    expect(verifyToken("unsubscribe", "rfq_1", token)).toBe(false)
    expect(verifyToken("resume", "rfq_2", token)).toBe(false)
    expect(verifyToken("resume", "rfq_1", undefined)).toBe(false)
  })
})

describe("email templates", () => {
  const quote = priceQuote({ product_type: "mailer-box", quantity: 1000 })

  it("renders the quote email with checkout link and escapes input", () => {
    const email = renderQuoteEmail({
      firstName: "<script>x</script>",
      rfqId: "01ABCDEFGHJKMNPQRSTVWXYZ012",
      quote,
      checkoutUrl:
        "https://packoasis.com/api/quote-checkout?cart_id=cart_1&country=us",
      unsubscribeUrl:
        "https://api.packoasis.com/packoasis/unsubscribe?rfq=1&token=t",
    })
    expect(email.subject).toContain("1,000")
    expect(email.html).toContain("Complete my order")
    expect(email.html).toContain("cart_id=cart_1&amp;country=us")
    expect(email.html).not.toContain("<script>x</script>")
    expect(email.text).toContain("Complete your order")
  })

  it("renders follow-up and sales emails", () => {
    const followup = renderFollowupEmail({
      firstName: "Sam",
      subject: "Hi",
      paragraphs: ["One", "Two"],
      quote,
      checkoutUrl: "https://x",
      unsubscribeUrl: "https://y",
    })
    expect(followup.html).toContain("<p>Two</p>")

    const sales = renderSalesLeadEmail({
      rfq: {
        id: "r1",
        contact_name: "Sam",
        email: "sam@brand.com",
        source: "instant_quote",
      },
      quote,
      profile: null,
      trail: null,
      adminUrl: "https://admin",
    })
    expect(sales.subject).toContain("Sam")
  })
})

describe("widget source", () => {
  it("is self-contained so it can be served via toString()", () => {
    const source = packoasisWidget.toString()
    expect(source.startsWith("function packoasisWidget")).toBe(true)
    expect(source).not.toMatch(
      /\brequire\(|\bexports\.|_define_property|_async_to_generator|__awaiter/
    )
    // Parses as a standalone script.
    expect(() => new Function(`return (${source})`)).not.toThrow()
  })

  it("serves a standalone script from GET /packoasis/widget.js", async () => {
    const js = await servedWidget()
    expect(js).toContain("PackOasisQuote")
    expect(() => new Script(js)).not.toThrow()
  })

  it("lets the host page lift the floating button", () => {
    const source = packoasisWidget.toString()
    expect(source).toContain("bottom:calc(20px + var(--po-fab-offset, 0px))")
    expect(source).toContain("bottom:calc(12px + var(--po-fab-offset, 0px))")
  })
})

let served: string | undefined
async function servedWidget() {
  if (!served) {
    await widgetRoute(
      {} as any,
      {
        setHeader: () => {},
        send: (body: string) => (served = body),
      } as any
    )
  }
  return served as string
}

// jsdom is not a backend dependency, so the browser tests below only run
// where it resolves (for example NODE_PATH=<dir with jsdom>/node_modules).
let jsdom: any = null
try {
  jsdom = require("jsdom")
} catch (e) {}
const describeDom = jsdom ? describe : describe.skip

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const usd = (amount: number) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: amount < 1 ? 3 : 2,
  }).format(amount)

describeDom("widget in the browser", () => {
  const windows: any[] = []
  afterEach(() => {
    windows.splice(0).forEach((win) => win.close())
  })

  async function boot(path: string, data: Record<string, string> = {}) {
    const dom = new jsdom.JSDOM(
      "<!doctype html><html><head><title>Page</title></head><body></body></html>",
      {
        url: `https://shop.example.com${path}`,
        runScripts: "outside-only",
        virtualConsole: new jsdom.VirtualConsole(),
      }
    )
    const win = dom.window
    windows.push(win)
    const requests: any[] = []
    const beacons: any[] = []
    win.fetch = (url: string, init: any) =>
      new Promise((resolve) => {
        requests.push({
          path: new URL(url).pathname,
          body: init && init.body ? JSON.parse(init.body) : null,
          reply: (json: any, status = 200) =>
            resolve({ ok: status < 400, status, json: async () => json }),
        })
      })
    win.navigator.sendBeacon = (_url: string, body: string) => {
      beacons.push(...JSON.parse(body).e)
      return true
    }
    const script = win.document.createElement("script")
    script.setAttribute("src", "https://api.example.com/packoasis/widget.js")
    Object.assign(script.dataset, { publishableKey: "pk_test" }, data)
    win.document.head.appendChild(script)
    win.eval(await servedWidget())
    const root = win.document.getElementById("packoasis-quote-widget")
      .shadowRoot
    const $ = (selector: string) => root.querySelector(selector)

    // The oldest unanswered request to path (estimates debounce up to 400 ms).
    async function request(path: string) {
      for (let i = 0; i < 100; i++) {
        const found = requests.find((r) => r.path === path && !r.taken)
        if (found) {
          found.taken = true
          return found
        }
        await sleep(10)
      }
      throw new Error(`no request to ${path}`)
    }
    async function answer(pending: any, json?: any, status?: number) {
      pending.reply(json || { quote: priceQuote(pending.body.specs) }, status)
      await sleep(0)
      return pending.body.specs ? priceQuote(pending.body.specs) : null
    }
    function type(selector: string, value: string) {
      const input = $(selector)
      input.value = value
      input.dispatchEvent(new win.Event("input", { bubbles: true }))
    }
    function navigate(path: string) {
      win.history.pushState({}, "", path)
      win.PackOasisQuote.onRoute()
    }
    async function openWithQuote() {
      win.PackOasisQuote.open()
      return answer(await request("/store/instant-quote/estimate"))
    }
    async function toContact() {
      const first = await openWithQuote()
      $('[data-act="next"]').click()
      return first
    }
    return {
      win,
      $,
      beacons,
      request,
      answer,
      type,
      navigate,
      openWithQuote,
      toContact,
    }
  }

  it("ignores an estimate for specs the buyer already changed", async () => {
    const w = await boot("/us/products/mailer-box")
    await w.openWithQuote()

    const rush = w.$('[data-field="rush"]')
    rush.checked = true
    rush.dispatchEvent(new w.win.Event("change", { bubbles: true }))
    const stale = await w.request("/store/instant-quote/estimate")
    w.type("#po-qty", "5000") // debounced, not sent yet
    await w.answer(stale)
    expect(w.$('[data-act="next"]').disabled).toBe(true)
    w.$('[data-act="next"]').click()
    expect(w.$("#po-name")).toBeNull()

    const fresh = await w.request("/store/instant-quote/estimate")
    expect(fresh.body.specs.quantity).toBe(5000)
    const quote = await w.answer(fresh)
    const cta = w.$('[data-act="next"]')
    expect(cta.disabled).toBe(false)
    expect(cta.textContent).toContain(usd(quote!.total))
    cta.click()
    expect(w.$('[data-form="contact"] [type="submit"]').textContent).toBe(
      `Continue to checkout · ${usd(quote!.total)}`
    )
  })

  it("shows no price or order button for invalid specs", async () => {
    const w = await boot("/us/products/mailer-box")
    await w.openWithQuote()
    w.type("#po-qty", "5000")
    const inFlight = await w.request("/store/instant-quote/estimate")
    w.type("#po-qty", "")
    await w.answer(inFlight)
    expect(w.$(".err").textContent).toBe("Enter how many pieces you need.")
    expect(w.$('[data-act="next"]')).toBeNull()
    expect(w.$(".total")).toBeNull()
  })

  it("quotes a zero gusset where the catalog allows it", async () => {
    const w = await boot("/custom-mailers.html")
    w.win.PackOasisQuote.open()
    const first = await w.request("/store/instant-quote/estimate")
    expect(first.body.specs.product_type).toBe("poly-mailer")
    expect(first.body.specs.dimensions).toEqual([10, 0, 13])
    await w.answer(first)
    expect(w.$(".err")).toBeNull()
    expect(w.$('[data-act="next"]').disabled).toBe(false)

    w.type('[data-dim="1"]', "-1")
    expect(w.$(".err").textContent).toBe(
      "Enter the gusset as a number of 0 or more."
    )
    w.type('[data-dim="1"]', "0")
    w.type('[data-dim="0"]', "0")
    expect(w.$(".err").textContent).toBe("Enter the width as a number above 0.")
  })

  it("drops a late AI parse once the buyer is on the contact step", async () => {
    const w = await boot("/us/products/mailer-box")
    const first = await w.openWithQuote()
    const ordered = w.$('[data-act="next"]').textContent
    w.$("#po-describe").value = "2000 mailer boxes 9x6x3"
    w.$('[data-act="describe"]').click()
    const parse = await w.request("/store/instant-quote/parse")

    w.$('[data-act="next"]').click()
    w.$("#po-name").value = "Jane Buyer"
    w.$("#po-email").value = "jane@brand.com"
    const parsed = priceQuote({
      product_type: "mailer-box",
      quantity: 2000,
      dimensions: [9, 6, 3],
    })
    await w.answer(parse, { quote: parsed, missing: [], notes: "" })
    expect(w.$("#po-name").value).toBe("Jane Buyer")
    expect(w.$('[data-form="contact"] [type="submit"]').textContent).toBe(
      ordered.replace("Order now", "Continue to checkout")
    )

    w.$('[data-form="contact"]').dispatchEvent(
      new w.win.Event("submit", { bubbles: true, cancelable: true })
    )
    const order = await w.request("/store/instant-quote/order")
    expect(order.body.specs.quantity).toBe(first!.quantity)

    w.$('[data-act="back"]').click()
    expect(w.$('[data-act="describe"]').textContent).toBe("Fill in")
  })

  it("lets an AI parse replace an estimate still in flight", async () => {
    const w = await boot("/us/products/mailer-box")
    await w.openWithQuote()
    w.$("#po-describe").value = "2000 mailer boxes 9x6x3"
    w.$('[data-act="describe"]').click()
    const parse = await w.request("/store/instant-quote/parse")
    w.type("#po-qty", "5000")
    const estimate = await w.request("/store/instant-quote/estimate")

    const parsed = priceQuote({
      product_type: "mailer-box",
      quantity: 2000,
      dimensions: [9, 6, 3],
    })
    await w.answer(parse, { quote: parsed, missing: [], notes: "" })
    await w.answer(estimate)
    expect(w.$("#po-qty").value).toBe("2000")
    expect(w.$('[data-act="next"]').disabled).toBe(false)
    expect(w.$('[data-act="next"]').textContent).toContain(usd(parsed.total))
  })

  it("re-enables the order form after a bfcache restore", async () => {
    const w = await boot("/us/products/mailer-box")
    const quote = await w.toContact()
    w.$("#po-name").value = "Jane Buyer"
    w.$("#po-email").value = "jane@brand.com"
    w.$('[data-form="contact"]').dispatchEvent(
      new w.win.Event("submit", { bubbles: true, cancelable: true })
    )
    const order = await w.request("/store/instant-quote/order")
    await w.answer(order, {
      rfq_id: "rfq_1",
      checkout_url: "https://shop.example.com/api/quote-checkout?cart_id=c",
    })
    const submit = w.$('[data-form="contact"] [type="submit"]')
    expect(submit.disabled).toBe(true)

    w.win.dispatchEvent(
      new w.win.PageTransitionEvent("pageshow", { persisted: true })
    )
    expect(submit.disabled).toBe(false)
    expect(submit.textContent).toBe(
      `Continue to checkout · ${usd(quote!.total)}`
    )
    expect(w.$("#po-name").value).toBe("Jane Buyer")
  })

  it("binds each order request to a fresh handoff cookie", async () => {
    const w = await boot("/us/products/mailer-box")
    const cookieWrites: string[] = []
    const cookie = Object.getOwnPropertyDescriptor(
      w.win.Document.prototype,
      "cookie"
    )!
    Object.defineProperty(w.win.document, "cookie", {
      configurable: true,
      get() {
        return cookie.get!.call(this)
      },
      set(value: string) {
        cookieWrites.push(value)
        cookie.set!.call(this, value)
      },
    })
    await w.toContact()
    w.$("#po-name").value = "Jane Buyer"
    w.$("#po-email").value = "jane@brand.com"
    const submit = () =>
      w
        .$('[data-form="contact"]')
        .dispatchEvent(
          new w.win.Event("submit", { bubbles: true, cancelable: true })
        )

    submit()
    const first = await w.request("/store/instant-quote/order")
    const handoff = first.body.handoff
    // 32 random bytes, base64url without padding.
    expect(handoff).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(cookieWrites).toEqual([
      `po_qn=${handoff}; path=/; max-age=1800; SameSite=Lax; Secure`,
    ])
    expect(w.win.document.cookie).toContain(`po_qn=${handoff}`)
    await w.answer(first, { message: "Try again" }, 400)

    submit()
    const second = await w.request("/store/instant-quote/order")
    expect(second.body.handoff).toMatch(/^[A-Za-z0-9_-]{43}$/)
    expect(second.body.handoff).not.toBe(handoff)
    expect(w.win.document.cookie).toContain(`po_qn=${second.body.handoff}`)
    expect(w.win.document.cookie).not.toContain(handoff)
  })

  it("preselects the configured ship-to country", async () => {
    for (const [country, expected] of [
      [undefined, "us"],
      ["ca", "ca"],
      ["GB", "gb"],
      ["de", "xx"],
    ]) {
      const w = await boot(
        "/us/products/mailer-box",
        country ? { country } : {}
      )
      await w.toContact()
      expect(w.$("#po-country").value).toBe(expected)
    }
  })

  it("re-evaluates the route after client-side navigation", async () => {
    const w = await boot("/us/products/mailer-box")
    const link = w.win.document.createElement("a")
    link.href = "/contact-us.html"
    link.textContent = "Need a custom project quote?"
    w.win.document.body.appendChild(link)
    expect(w.$(".fab")).not.toBeNull()

    w.navigate("/us/checkout")
    expect(w.$(".fab")).toBeNull()
    link.click()
    expect(w.$(".dialog")).toBeNull()
    await sleep(150)

    w.navigate("/us/products/custom-tissue-paper")
    w.navigate("/us/products/custom-tissue-paper") // idempotent
    expect(w.$(".fab")).not.toBeNull()
    await sleep(150)
    w.win.dispatchEvent(new w.win.Event("pagehide"))
    const views = w.beacons.filter((e) => e.t === "page_view")
    expect(views.map((e) => e.p)).toEqual([
      "/us/products/mailer-box",
      "/us/checkout",
      "/us/products/custom-tissue-paper",
    ])
    expect(views[2]).toMatchObject({
      pt: "tissue-paper",
      r: "https://shop.example.com/us/checkout",
    })

    // The configurator starts on the new page's product, like a page load.
    link.click()
    expect(w.$(".dialog")).not.toBeNull()
    const estimate = await w.request("/store/instant-quote/estimate")
    expect(estimate.body.specs.product_type).toBe("tissue-paper")

    // Landing on a quiet route (back button) closes the dialog.
    w.win.history.pushState({}, "", "/us/cart")
    w.win.dispatchEvent(new w.win.PopStateEvent("popstate"))
    expect(w.$(".dialog")).toBeNull()
    expect(w.$(".fab")).toBeNull()
  })

  it("drops an estimate for the previous page's product", async () => {
    const w = await boot("/us/products/mailer-box")
    await w.openWithQuote()
    w.type("#po-qty", "5000")
    const stale = await w.request("/store/instant-quote/estimate")
    w.win.PackOasisQuote.close()

    w.navigate("/us/products/custom-tissue-paper")
    await w.answer(stale)
    w.win.PackOasisQuote.open()
    expect(w.$(".total")).toBeNull()
    expect(w.$('[data-act="next"]')).toBeNull()

    const fresh = await w.request("/store/instant-quote/estimate")
    expect(fresh.body.specs.product_type).toBe("tissue-paper")
    const quote = await w.answer(fresh)
    expect(w.$(".total").textContent).toBe(usd(quote!.total))
    expect(w.$('[data-act="next"]').disabled).toBe(false)
  })
})
