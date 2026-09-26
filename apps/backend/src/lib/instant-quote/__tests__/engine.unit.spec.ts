import { DEFAULT_PRICING } from "../catalog"
import {
  loadPricingConfig,
  normalizeSpecs,
  priceQuote,
  productTypeForPath,
  publicCatalog,
  QuoteInputError,
} from "../engine"

const NOW = new Date("2026-09-28T12:00:00Z") // a Monday

describe("instant quote engine", () => {
  it("prices the default mailer box at MOQ", () => {
    const quote = priceQuote({ product_type: "mailer-box" }, { now: NOW })

    expect(quote.instant).toBe(true)
    expect(quote.quantity).toBe(500)
    expect(quote.specs.dimensions).toEqual([10, 8, 4])
    expect(quote.currency_code).toBe("usd")
    expect(quote.total).toBeGreaterThan(0)
    expect(quote.unit_price).toBeCloseTo(quote.total / quote.quantity, 3)
    // breakdown lines add up to the total
    const sum = quote.breakdown.reduce((acc, line) => acc + line.amount, 0)
    expect(sum).toBeCloseTo(quote.total, 0)
    expect(quote.summary).toContain("Custom Mailer Box")
    expect(quote.valid_until).toBe("2026-10-12")
  })

  it("keeps unit prices inside sane market ranges at MOQ", () => {
    // Guards against typos in the catalog: USD per piece at MOQ, default specs.
    const ranges: Record<string, [number, number]> = {
      "mailer-box": [2, 6],
      "shipping-box": [1.5, 5],
      "folding-carton": [0.3, 1.5],
      "rigid-box": [3, 9],
      "stand-up-pouch": [0.15, 0.8],
      "paper-bag": [1, 3.5],
      label: [0.05, 0.4],
      "tissue-paper": [0.08, 0.4],
      "box-insert": [0.15, 0.8],
      "poly-mailer": [0.12, 0.5],
      "packing-tape": [2, 6],
    }

    for (const [id, [min, max]] of Object.entries(ranges)) {
      const quote = priceQuote({ product_type: id }, { now: NOW })
      expect({
        id,
        unit: quote.unit_price,
        ok: quote.unit_price >= min && quote.unit_price <= max,
      }).toEqual({
        id,
        unit: quote.unit_price,
        ok: true,
      })
    }
  })

  it("gives volume discounts on larger runs", () => {
    const small = priceQuote(
      { product_type: "folding-carton", quantity: 1000 },
      { now: NOW }
    )
    const large = priceQuote(
      { product_type: "folding-carton", quantity: 10000 },
      { now: NOW }
    )

    expect(large.unit_price).toBeLessThan(small.unit_price)
    expect(large.total).toBeGreaterThan(small.total)

    const tierQuantities = small.tiers.map((tier) => tier.quantity)
    expect(tierQuantities[0]).toBe(1000)
    expect([...tierQuantities].sort((a, b) => a - b)).toEqual(tierQuantities)
    for (let i = 1; i < small.tiers.length; i++) {
      expect(small.tiers[i].unit_price).toBeLessThan(
        small.tiers[i - 1].unit_price
      )
      expect(small.tiers[i].savings_pct).toBeGreaterThan(0)
    }
  })

  it("includes the MOQ tier when a larger quantity is requested", () => {
    const quote = priceQuote(
      { product_type: "mailer-box", quantity: 3000 },
      { now: NOW }
    )
    const quantities = quote.tiers.map((tier) => tier.quantity)
    expect(quantities[0]).toBe(500)
    expect(quantities).toContain(3000)
    expect(
      quote.tiers.find((tier) => tier.quantity === 500)!.savings_pct
    ).toBeLessThan(0)
  })

  it("raises quantities below MOQ and warns", () => {
    const quote = priceQuote(
      { product_type: "rigid-box", quantity: 50 },
      { now: NOW }
    )
    expect(quote.quantity).toBe(500)
    expect(quote.warnings.join(" ")).toMatch(/minimum/)
  })

  it("converts metric dimensions and clamps out-of-range sizes", () => {
    const { specs, warnings } = normalizeSpecs({
      product_type: "mailer-box",
      dimensions: [254, 203.2, 101.6],
      unit: "mm",
    })
    expect(specs.dimensions).toEqual([10, 8, 4])
    expect(warnings).toHaveLength(0)

    const clamped = normalizeSpecs({
      product_type: "mailer-box",
      dimensions: [100, 8, 4],
    })
    expect(clamped.specs.dimensions[0]).toBe(30)
    expect(clamped.warnings[0]).toMatch(/Length adjusted/)
  })

  it("falls back to the default print and drops unsupported finishes", () => {
    const { specs, warnings } = normalizeSpecs({
      product_type: "tissue-paper",
      print: "cmyk_both",
      finishes: ["foil", "not-a-finish"],
    })
    expect(specs.print).toBe("one_color")
    expect(specs.finishes).toEqual([])
    expect(warnings.join(" ")).toMatch(/not available/)
  })

  it("resolves conflicting laminations", () => {
    const { specs } = normalizeSpecs({
      product_type: "folding-carton",
      finishes: ["gloss_lamination", "matte_lamination", "foil"],
    })
    expect(specs.finishes).toEqual(["matte_lamination", "foil"])

    const softTouch = normalizeSpecs({
      product_type: "rigid-box",
      finishes: ["matte_lamination", "soft_touch"],
    })
    expect(softTouch.specs.finishes).toEqual(["soft_touch"])
  })

  it("charges more and ships faster for rush orders", () => {
    const standard = priceQuote(
      { product_type: "mailer-box", quantity: 1000 },
      { now: NOW }
    )
    const rush = priceQuote(
      { product_type: "mailer-box", quantity: 1000, rush: true },
      { now: NOW }
    )
    expect(rush.total).toBeGreaterThan(standard.total)
    expect(rush.lead_time.production_days[1]).toBeLessThan(
      standard.lead_time.production_days[1]
    )
    expect(rush.estimated_delivery.to < standard.estimated_delivery.to).toBe(
      true
    )
  })

  it("adds finish lead time and setup", () => {
    const plain = priceQuote({ product_type: "rigid-box" }, { now: NOW })
    const foil = priceQuote(
      { product_type: "rigid-box", finishes: ["foil"] },
      { now: NOW }
    )
    expect(foil.total).toBeGreaterThan(plain.total)
    expect(foil.lead_time.production_days[0]).toBe(
      plain.lead_time.production_days[0] + 2
    )
    expect(foil.breakdown.map((line) => line.label)).toContain(
      "Foil stamping (1 location)"
    )
  })

  it("rejects unknown product types", () => {
    expect(() => priceQuote({ product_type: "spaceship" })).toThrow(
      QuoteInputError
    )
  })

  it("applies overrides from INSTANT_QUOTE_PRICING_JSON", () => {
    const override = JSON.stringify({
      margin_multiplier: 2,
      min_order_total: 5000,
    })
    const config = loadPricingConfig(override)
    expect(config.margin_multiplier).toBe(2)
    expect(config.product_types["mailer-box"].moq).toBe(
      DEFAULT_PRICING.product_types["mailer-box"].moq
    )

    const quote = priceQuote({ product_type: "label" }, { config, now: NOW })
    expect(quote.total).toBe(5000)
    expect(quote.breakdown.map((line) => line.label)).toContain(
      "Minimum order adjustment"
    )

    expect(loadPricingConfig("{not json")).toEqual(DEFAULT_PRICING)
  })

  it("marks structural formats as not instant", () => {
    const quote = priceQuote({ product_type: "floor-display" }, { now: NOW })
    expect(quote.instant).toBe(false)
    expect(quote.assumptions[0]).toMatch(/structural review/)
  })

  it("publishes a catalog with starting prices", () => {
    const catalog = publicCatalog()
    expect(catalog.product_types.length).toBe(
      Object.keys(DEFAULT_PRICING.product_types).length
    )
    for (const type of catalog.product_types) {
      expect(type.price_from.unit_price).toBeGreaterThan(0)
      expect(type.price_from.unit_price).toBeLessThanOrEqual(
        type.price_at_moq.total / type.moq + 1e-6
      )
      expect(type).not.toHaveProperty("base_unit_cost")
    }
  })
})

describe("productTypeForPath", () => {
  it.each([
    ["/custom-mailers.html", "poly-mailer"],
    ["/custom-printed-shipping-boxes.html", "mailer-box"],
    ["/custom-printed-corrugated-boxes.html", "shipping-box"],
    ["/custom-printed-luxury-paper-bags.html", "paper-bag"],
    ["/custom-reusable-shopping-bags.html", null],
    ["/custom-rigid-setup-boxes.html", "rigid-box"],
    ["/custom-cosmetic-packaging-boxes.html", "folding-carton"],
    ["/custom-pouches.html", "stand-up-pouch"],
    ["/printed-tape.html", "packing-tape"],
    ["/custom-cardboard-displays.html", "floor-display"],
    ["/about-us.html", null],
  ])("%s -> %s", (path, expected) => {
    expect(productTypeForPath(path)).toBe(expected)
  })
})
