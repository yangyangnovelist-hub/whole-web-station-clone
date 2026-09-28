import { priceQuote } from "../../instant-quote/engine"
import { heuristicParseSpecs } from "../parse-specs"

describe("heuristicParseSpecs", () => {
  it("reads type, size, quantity, print and finishes from free text", () => {
    const specs = heuristicParseSpecs(
      "We need 2,500 kraft mailer boxes 12 x 9 x 4 inches, full color outside, soft touch with gold foil logo, rush please"
    )
    expect(specs.product_type).toBe("mailer-box")
    expect(specs.dimensions).toEqual([12, 9, 4])
    expect(specs.unit).toBe("in")
    expect(specs.quantity).toBe(2500)
    expect(specs.print).toBe("cmyk_outside")
    expect(specs.finishes).toEqual(
      expect.arrayContaining(["soft_touch", "foil"])
    )
    expect(specs.material).toBe("kraft_e_flute")
    expect(specs.rush).toBe(true)
    expect(specs.missing).toEqual([])
  })

  it("handles metric sizes, k quantities and one-color print", () => {
    const specs = heuristicParseSpecs(
      "stand up pouch with zipper 15x22x8 cm, 20k pcs, one color logo"
    )
    expect(specs.product_type).toBe("stand-up-pouch")
    expect(specs.dimensions).toEqual([15, 22, 8])
    expect(specs.unit).toBe("cm")
    expect(specs.quantity).toBe(20000)
    expect(specs.print).toBe("one_color")
    expect(specs.addons).toEqual(["zipper"])
  })

  it("uses the page hint and reports what is missing", () => {
    const specs = heuristicParseSpecs(
      "something nice with a matte finish",
      "rigid-box"
    )
    expect(specs.product_type).toBe("rigid-box")
    expect(specs.finishes).toEqual(["matte_lamination"])
    expect(specs.missing).toEqual(["dimensions", "quantity"])
  })

  it("reads qty: notation", () => {
    expect(heuristicParseSpecs("labels 3x3 qty: 5000").quantity).toBe(5000)
  })

  it.each([
    ["mailer box 25 x 20 x 10 (cm), 1000 pcs", [25, 20, 10], "cm"],
    ["mailer box 25x20x10 centimeters, 1000 pcs", [25, 20, 10], "cm"],
    ["mailer box 25x20x10 centimetres, 1000 pcs", [25, 20, 10], "cm"],
    ["box 250x200x100 millimeters qty 1000", [250, 200, 100], "mm"],
    ["mailer box 25 x 20 x 10 cm. 1000 pcs", [25, 20, 10], "cm"],
  ])("reads the unit in %j", (text, dimensions, unit) => {
    const specs = heuristicParseSpecs(text)
    expect(specs.dimensions).toEqual(dimensions)
    expect(specs.unit).toBe(unit)
    expect(specs.quantity).toBe(1000)
    // 25 x 20 x 10 cm is roughly a 9.84 x 7.87 x 3.94 in box
    expect(priceQuote(specs).specs.dimensions).toEqual([9.84, 7.87, 3.94])
  })

  it("matches product keywords on whole words only", () => {
    // "printing" must not match the tin-box keyword "tin"
    expect(
      heuristicParseSpecs(
        "1000 custom boxes 10x8x4 with full color printing",
        "mailer-box"
      ).product_type
    ).toBe("mailer-box")
    expect(
      heuristicParseSpecs("full color printing on 500 tinted boxes")
        .product_type
    ).not.toBe("tin-box")
    // "resealable", "sealed" and "sealing" must not match the label keyword
    expect(
      heuristicParseSpecs("resealable bags 6x9x3, 10000 pcs").product_type
    ).not.toBe("label")
    expect(
      heuristicParseSpecs("vacuum sealed bags 6x9, 5000 pcs").product_type
    ).not.toBe("label")
    expect(
      heuristicParseSpecs("heat sealing film 6x9, 5000 pcs").product_type
    ).not.toBe("label")
    expect(
      heuristicParseSpecs("sealing tape 2 in, 100 rolls").product_type
    ).toBe("packing-tape")
    // plurals still count
    expect(heuristicParseSpecs("custom seals 2x2, 5000 pcs").product_type).toBe(
      "label"
    )
    expect(heuristicParseSpecs("2000 tins 4x3x1").product_type).toBe("tin-box")
    expect(
      heuristicParseSpecs("custom shipping boxes 12x10x8, 500 pcs").product_type
    ).toBe("shipping-box")
  })

  it.each([
    ["product labeling 2x3, 5000 pcs", "label"],
    ["custom labelling for jars 2x3 5000 pcs", "label"],
    ["labeled jars 2x3 5000", "label"],
    ["labelled bottles 2x4 5000", "label"],
    ["stickered bottles 2x3 5000", "label"],
    ["branded wrapping 20x30 10000 sheets", "tissue-paper"],
    ["taped boxes", "packing-tape"],
    ["custom taping, 50 rolls", "packing-tape"],
  ])("matches -ed and -ing forms of keywords in %j", (text, type) => {
    expect(heuristicParseSpecs(text).product_type).toBe(type)
  })

  it("keeps the page hint when another type only ties its score", () => {
    const text = "mailer insert 8x6, 1000 pcs"
    expect(heuristicParseSpecs(text).product_type).toBe("mailer-box")
    expect(heuristicParseSpecs(text, "box-insert").product_type).toBe(
      "box-insert"
    )
  })

  it("reads two-number bag sizes as width x height with the default gusset", () => {
    const mailer = heuristicParseSpecs("10x13 poly mailers, 10000 pcs, 1 color")
    expect(mailer.product_type).toBe("poly-mailer")
    expect(mailer.dimensions).toEqual([10, 0, 13])
    const quote = priceQuote(mailer)
    expect(quote.specs.dimensions).toEqual([10, 0, 13])
    expect(quote.warnings).toEqual([])

    const bag = heuristicParseSpecs("paper bags 10x13, 1000 pcs")
    expect(bag.product_type).toBe("paper-bag")
    expect(bag.dimensions).toEqual([10, 5, 13])

    const metric = heuristicParseSpecs("paper bag 25x33 cm, 1000 pcs")
    expect(metric.unit).toBe("cm")
    expect(metric.dimensions).toEqual([25, 12.7, 33])
    expect(priceQuote(metric).specs.dimensions[1]).toBe(5)
  })

  it.each(["expedite", "expedited", "expediting"])(
    "flags %s as rush",
    (word) => {
      expect(heuristicParseSpecs(`${word}: 1000 mailer boxes`).rush).toBe(true)
    }
  )

  it.each(["constructor", "toString", "__proto__"])(
    "ignores the Object.prototype key %s as a page hint",
    (hint) => {
      const specs = heuristicParseSpecs("something nice, 1000 pcs", hint)
      expect(specs.product_type).toBe("mailer-box")
      expect(() => priceQuote(specs)).not.toThrow()
    }
  )

  it("falls back to an offered type when mailer-box is removed by an override", () => {
    const previous = process.env.INSTANT_QUOTE_PRICING_JSON
    process.env.INSTANT_QUOTE_PRICING_JSON = JSON.stringify({
      product_types: { "mailer-box": null },
    })
    try {
      const specs = heuristicParseSpecs("something nice, 1000 pcs")
      expect(specs.product_type).not.toBe("mailer-box")
      expect(() => priceQuote(specs)).not.toThrow()
    } finally {
      if (previous === undefined) {
        delete process.env.INSTANT_QUOTE_PRICING_JSON
      } else {
        process.env.INSTANT_QUOTE_PRICING_JSON = previous
      }
    }
  })
})
