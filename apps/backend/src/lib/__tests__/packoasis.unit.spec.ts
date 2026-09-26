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
})
