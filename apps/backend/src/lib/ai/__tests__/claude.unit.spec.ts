import Anthropic from "@anthropic-ai/sdk"
import { buildLeadProfile } from "../lead-profile"
import { parseSpecsWithAI } from "../parse-specs"
import { setClaudeClientForTests } from "../claude"

type Captured = { url: string; headers: Record<string, string>; body: any }

function mockClient(
  reply: (body: any) => { stop_reason: string; text: string }
) {
  const calls: Captured[] = []
  const client = new Anthropic({
    apiKey: "sk-ant-test",
    maxRetries: 0,
    fetch: (async (url: string, init: any) => {
      const body = JSON.parse(init.body)
      const headers: Record<string, string> = {}
      new Headers(init.headers).forEach((value, key) => (headers[key] = value))
      calls.push({ url: String(url), headers, body })
      const { stop_reason, text } = reply(body)
      return new Response(
        JSON.stringify({
          id: "msg_test",
          type: "message",
          role: "assistant",
          model: body.model,
          content: text ? [{ type: "text", text }] : [],
          stop_reason,
          stop_sequence: null,
          usage: { input_tokens: 10, output_tokens: 20 },
        }),
        { status: 200, headers: { "content-type": "application/json" } }
      )
    }) as any,
  })
  return { client, calls }
}

describe("Claude integration (mocked transport)", () => {
  const original = process.env.ANTHROPIC_API_KEY
  beforeAll(() => {
    process.env.ANTHROPIC_API_KEY = "sk-ant-test"
  })
  afterAll(() => {
    process.env.ANTHROPIC_API_KEY = original
    setClaudeClientForTests(undefined)
  })

  it("sends a structured-output request with server-side fallbacks and parses it", async () => {
    const { client, calls } = mockClient(() => ({
      stop_reason: "end_turn",
      text: JSON.stringify({
        product_type: "mailer-box",
        dimensions: [12, 9, 4],
        unit: "in",
        quantity: 2000,
        material: "kraft_e_flute",
        print: "cmyk_outside",
        finishes: ["soft_touch", "foil"],
        addons: [],
        rush: false,
        notes: "Gold foil logo on the lid.",
        missing: [],
      }),
    }))
    setClaudeClientForTests(client)

    const specs = await parseSpecsWithAI(
      "2,000 kraft mailers 12x9x4, full color, soft touch, gold foil",
      "folding-carton"
    )
    expect(specs?.source).toBe("ai")
    expect(specs?.quantity).toBe(2000)
    expect(specs?.finishes).toEqual(["soft_touch", "foil"])

    const [call] = calls
    expect(call.url).toContain("/v1/messages")
    expect(call.headers["anthropic-beta"]).toContain(
      "server-side-fallback-2026-07-01"
    )
    expect(call.body.model).toBe("claude-opus-5")
    expect(call.body.fallbacks).toBe("default")
    expect(call.body.output_config.effort).toBe("low")
    expect(call.body.output_config.format.type).toBe("json_schema")
    expect(
      Object.keys(call.body.output_config.format.schema.properties)
    ).toEqual(
      expect.arrayContaining([
        "product_type",
        "dimensions",
        "quantity",
        "finishes",
        "missing",
      ])
    )
    expect(call.body.messages[0].content).toContain("<customer_request>")
    expect(call.body.messages[0].content).toContain(
      '"folding-carton" product page'
    )
  })

  it("falls back when the request is declined", async () => {
    const { client } = mockClient(() => ({ stop_reason: "refusal", text: "" }))
    setClaudeClientForTests(client)
    expect(
      await parseSpecsWithAI("anything", null, { warn: () => {} })
    ).toBeNull()

    const profile = await buildLeadProfile(
      {
        contact_name: "Sam",
        email: "sam@brand.com",
        company: "Brand",
        quote: { summary: "x", total: 5000, currency_code: "usd" },
      },
      { warn: () => {} }
    )
    expect(profile.source).toBe("heuristic")
    expect(profile.lead_score).toBeGreaterThan(0)
  })

  it("clamps and sanitizes AI lead profiles", async () => {
    const { client } = mockClient(() => ({
      stop_reason: "end_turn",
      text: JSON.stringify({
        company_name: "Brand Co",
        industry: "Candles",
        company_summary: "Makes soy candles.",
        products_they_sell: ["candles"],
        likely_packaging_needs: ["rigid boxes"],
        recommended_product_types: ["rigid-box"],
        company_size_estimate: "small",
        lead_score: 140,
        score_reasons: ["High quote value"],
        next_best_action: "Call today",
        personalization_hook:
          "Love your lavender line at https://brand.example, only $5!",
      }),
    }))
    setClaudeClientForTests(client)
    const profile = await buildLeadProfile({
      contact_name: "Sam",
      email: "sam@brand.com",
    })
    expect(profile.source).toBe("ai")
    expect(profile.lead_score).toBe(100)
    expect(profile.lead_grade).toBe("A")
    expect(profile.personalization_hook).not.toMatch(/https?:|\$5/)
  })
})
