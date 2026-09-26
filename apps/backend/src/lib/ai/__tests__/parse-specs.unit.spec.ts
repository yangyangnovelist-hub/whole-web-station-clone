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
})
