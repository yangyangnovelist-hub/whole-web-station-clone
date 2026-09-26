/**
 * Instant-quote catalog: every number the pricing engine uses lives here.
 *
 * The defaults are calibrated against public price points for custom printed
 * packaging in the US market (USD, factory-direct, freight to the contiguous
 * US included). They are a starting point, not PackOasis' real cost sheet:
 * override any of them in production with INSTANT_QUOTE_PRICING_JSON (deep
 * merged over DEFAULT_PRICING) before relying on instant orders.
 *
 * Units: dimensions in inches, area rates in USD per square inch, per-unit
 * amounts in USD per piece, setup amounts in USD per order.
 */

export type Shape = "box" | "bag" | "pouch" | "flat" | "unit"

export type PrintOptionId = "none" | "one_color" | "cmyk_outside" | "cmyk_both"

export type FinishId =
  | "matte_lamination"
  | "gloss_lamination"
  | "soft_touch"
  | "foil"
  | "emboss"
  | "spot_uv"
  | "window"

export type AddonId = "insert" | "zipper"

export type PrintRate = {
  per_sq_in: number
  per_unit?: number
  setup: number
}

export type MaterialOption = {
  id: string
  label: string
  per_sq_in: number
}

export type ProductTypeConfig = {
  id: string
  label: string
  category: string
  /** false = no instant price; the widget collects an RFQ instead */
  instant: boolean
  shape: Shape
  moq: number
  max_quantity: number
  dimension_labels: string[]
  default_dimensions: number[]
  min_dimensions: number[]
  max_dimensions: number[]
  /** Flat blank area / finished surface area (flaps, glue tabs, waste) */
  blank_factor: number
  materials: MaterialOption[]
  default_material: string
  base_unit_cost: number
  print: Partial<Record<PrintOptionId, PrintRate>>
  default_print: PrintOptionId
  finishes: FinishId[]
  addons: AddonId[]
  /** Cutting die / tooling charged once per order */
  structural_setup: number
  /** Business days in production after proof approval */
  production_days: [number, number]
  /** Unit cost multiplier = max(volume_floor, (qty / moq) ^ -volume_exponent) */
  volume_exponent: number
  volume_floor: number
  keywords: string[]
  description: string
}

export type FinishConfig = {
  id: FinishId
  label: string
  per_sq_in?: number
  per_unit?: number
  setup?: number
  extra_days?: number
}

export type AddonConfig = {
  id: AddonId
  label: string
  per_sq_in?: number
  per_unit?: number
  setup?: number
}

export type PricingConfig = {
  version: string
  currency_code: string
  margin_multiplier: number
  rush_multiplier: number
  rush_lead_time_factor: number
  min_order_total: number
  quote_valid_days: number
  shipping_days: [number, number]
  freight_included: boolean
  print_labels: Record<PrintOptionId, string>
  finishes: Record<FinishId, FinishConfig>
  addons: Record<AddonId, AddonConfig>
  product_types: Record<string, ProductTypeConfig>
}

const CARTON_FINISHES: FinishId[] = [
  "matte_lamination",
  "gloss_lamination",
  "soft_touch",
  "foil",
  "emboss",
  "spot_uv",
]

export const DEFAULT_PRICING: PricingConfig = {
  version: "2026-09-26",
  currency_code: "usd",
  margin_multiplier: 1.35,
  rush_multiplier: 1.2,
  rush_lead_time_factor: 0.6,
  min_order_total: 250,
  quote_valid_days: 14,
  shipping_days: [5, 12],
  freight_included: true,
  print_labels: {
    none: "Unprinted",
    one_color: "1-color print (outside)",
    cmyk_outside: "Full color CMYK (outside)",
    cmyk_both: "Full color CMYK (inside + outside)",
  },
  finishes: {
    matte_lamination: {
      id: "matte_lamination",
      label: "Matte lamination",
      per_sq_in: 0.001,
    },
    gloss_lamination: {
      id: "gloss_lamination",
      label: "Gloss lamination",
      per_sq_in: 0.0008,
    },
    soft_touch: {
      id: "soft_touch",
      label: "Soft-touch lamination",
      per_sq_in: 0.002,
      extra_days: 2,
    },
    foil: {
      id: "foil",
      label: "Foil stamping (1 location)",
      per_unit: 0.08,
      setup: 180,
      extra_days: 2,
    },
    emboss: {
      id: "emboss",
      label: "Embossing / debossing (1 location)",
      per_unit: 0.1,
      setup: 220,
      extra_days: 2,
    },
    spot_uv: {
      id: "spot_uv",
      label: "Spot UV",
      per_unit: 0.05,
      setup: 150,
      extra_days: 1,
    },
    window: {
      id: "window",
      label: "Window patch (PET)",
      per_unit: 0.06,
      setup: 120,
      extra_days: 1,
    },
  },
  addons: {
    insert: {
      id: "insert",
      label: "Custom paperboard insert",
      per_sq_in: 0.0015,
      setup: 150,
    },
    zipper: {
      id: "zipper",
      label: "Resealable zipper",
      per_unit: 0.03,
    },
  },
  product_types: {
    "mailer-box": {
      id: "mailer-box",
      label: "Custom Mailer Box",
      category: "Corrugated",
      instant: true,
      shape: "box",
      moq: 500,
      max_quantity: 100000,
      dimension_labels: ["Length", "Width", "Height"],
      default_dimensions: [10, 8, 4],
      min_dimensions: [3, 3, 1],
      max_dimensions: [30, 24, 16],
      blank_factor: 1.45,
      materials: [
        {
          id: "white_e_flute",
          label: "White E-flute corrugated",
          per_sq_in: 0.0022,
        },
        {
          id: "kraft_e_flute",
          label: "Kraft E-flute corrugated",
          per_sq_in: 0.002,
        },
        {
          id: "white_b_flute",
          label: "White B-flute (heavier)",
          per_sq_in: 0.0028,
        },
      ],
      default_material: "white_e_flute",
      base_unit_cost: 0.35,
      print: {
        none: { per_sq_in: 0, setup: 0 },
        one_color: { per_sq_in: 0.0012, setup: 150 },
        cmyk_outside: { per_sq_in: 0.003, setup: 250 },
        cmyk_both: { per_sq_in: 0.0052, setup: 400 },
      },
      default_print: "cmyk_outside",
      finishes: [
        "matte_lamination",
        "gloss_lamination",
        "soft_touch",
        "foil",
        "spot_uv",
      ],
      addons: ["insert"],
      structural_setup: 180,
      production_days: [10, 15],
      volume_exponent: 0.16,
      volume_floor: 0.6,
      keywords: [
        "mailer",
        "mailer box",
        "tuck top",
        "subscription box",
        "shipping mailer",
        "ecommerce box",
      ],
      description:
        "Die-cut corrugated mailer (roll end tuck top) for e-commerce, subscription and PR kits.",
    },
    "shipping-box": {
      id: "shipping-box",
      label: "Custom Shipping Box (RSC)",
      category: "Corrugated",
      instant: true,
      shape: "box",
      moq: 500,
      max_quantity: 100000,
      dimension_labels: ["Length", "Width", "Height"],
      default_dimensions: [12, 10, 8],
      min_dimensions: [4, 4, 2],
      max_dimensions: [48, 40, 40],
      blank_factor: 1.25,
      materials: [
        {
          id: "kraft_single_wall",
          label: "Kraft single-wall (32 ECT)",
          per_sq_in: 0.0016,
        },
        {
          id: "white_single_wall",
          label: "White single-wall (32 ECT)",
          per_sq_in: 0.0019,
        },
        {
          id: "kraft_double_wall",
          label: "Kraft double-wall (48 ECT)",
          per_sq_in: 0.0027,
        },
      ],
      default_material: "kraft_single_wall",
      base_unit_cost: 0.25,
      print: {
        none: { per_sq_in: 0, setup: 0 },
        one_color: { per_sq_in: 0.0008, setup: 150 },
        cmyk_outside: { per_sq_in: 0.0024, setup: 300 },
        cmyk_both: { per_sq_in: 0.0042, setup: 450 },
      },
      default_print: "one_color",
      finishes: [],
      addons: ["insert"],
      structural_setup: 0,
      production_days: [10, 15],
      volume_exponent: 0.14,
      volume_floor: 0.62,
      keywords: [
        "shipping box",
        "shipper",
        "corrugated box",
        "rsc",
        "carton box",
        "moving box",
        "outer box",
      ],
      description:
        "Regular slotted corrugated shipping carton for fulfillment and bulk shipping.",
    },
    "folding-carton": {
      id: "folding-carton",
      label: "Custom Folding Carton",
      category: "Folding Carton",
      instant: true,
      shape: "box",
      moq: 1000,
      max_quantity: 500000,
      dimension_labels: ["Length", "Width", "Height"],
      default_dimensions: [4, 2, 6],
      min_dimensions: [1, 1, 1],
      max_dimensions: [16, 12, 16],
      blank_factor: 1.35,
      materials: [
        { id: "sbs_18pt", label: "SBS C1S 18pt paperboard", per_sq_in: 0.0009 },
        {
          id: "kraft_18pt",
          label: "Kraft CUK 18pt paperboard",
          per_sq_in: 0.001,
        },
        {
          id: "sbs_24pt",
          label: "SBS C1S 24pt (rigid feel)",
          per_sq_in: 0.0013,
        },
      ],
      default_material: "sbs_18pt",
      base_unit_cost: 0.06,
      print: {
        none: { per_sq_in: 0, setup: 0 },
        one_color: { per_sq_in: 0.0006, setup: 150 },
        cmyk_outside: { per_sq_in: 0.0012, setup: 250 },
        cmyk_both: { per_sq_in: 0.0021, setup: 400 },
      },
      default_print: "cmyk_outside",
      finishes: [...CARTON_FINISHES, "window"],
      addons: ["insert"],
      structural_setup: 150,
      production_days: [8, 12],
      volume_exponent: 0.16,
      volume_floor: 0.55,
      keywords: [
        "folding carton",
        "carton",
        "product box",
        "retail box",
        "tuck end",
        "reverse tuck",
        "cosmetic box",
        "paperboard box",
      ],
      description:
        "Printed paperboard retail carton (reverse / straight tuck end) for cosmetics, supplements, food and consumer goods.",
    },
    "rigid-box": {
      id: "rigid-box",
      label: "Custom Rigid Setup Box",
      category: "Rigid",
      instant: true,
      shape: "box",
      moq: 500,
      max_quantity: 50000,
      dimension_labels: ["Length", "Width", "Height"],
      default_dimensions: [8, 6, 3],
      min_dimensions: [2, 2, 1],
      max_dimensions: [20, 16, 10],
      blank_factor: 1.2,
      materials: [
        {
          id: "chipboard_art_paper",
          label: "1200gsm chipboard + art paper wrap",
          per_sq_in: 0.0065,
        },
        {
          id: "chipboard_specialty",
          label: "1200gsm chipboard + specialty paper wrap",
          per_sq_in: 0.0085,
        },
      ],
      default_material: "chipboard_art_paper",
      base_unit_cost: 0.9,
      print: {
        none: { per_sq_in: 0, setup: 0 },
        one_color: { per_sq_in: 0.001, setup: 150 },
        cmyk_outside: { per_sq_in: 0.0025, setup: 250 },
        cmyk_both: { per_sq_in: 0.004, setup: 400 },
      },
      default_print: "cmyk_outside",
      finishes: CARTON_FINISHES,
      addons: ["insert"],
      structural_setup: 250,
      production_days: [15, 22],
      volume_exponent: 0.12,
      volume_floor: 0.65,
      keywords: [
        "rigid",
        "setup box",
        "luxury box",
        "magnetic",
        "two piece",
        "lid and base",
        "gift box",
        "jewelry box",
      ],
      description:
        "Premium chipboard rigid box (lid & base or magnetic closure) for luxury, gifting and jewelry.",
    },
    "stand-up-pouch": {
      id: "stand-up-pouch",
      label: "Custom Stand-up Pouch",
      category: "Flexible Packaging",
      instant: true,
      shape: "pouch",
      moq: 10000,
      max_quantity: 1000000,
      dimension_labels: ["Width", "Height", "Bottom gusset"],
      default_dimensions: [6, 9, 3],
      min_dimensions: [2, 3, 0],
      max_dimensions: [16, 20, 6],
      blank_factor: 1.05,
      materials: [
        { id: "pet_pe", label: "Matte PET/PE laminate", per_sq_in: 0.0011 },
        {
          id: "kraft_pla",
          label: "Kraft paper / PLA (compostable)",
          per_sq_in: 0.0016,
        },
        {
          id: "foil_barrier",
          label: "PET/AL/PE high-barrier foil",
          per_sq_in: 0.0015,
        },
      ],
      default_material: "pet_pe",
      base_unit_cost: 0.03,
      print: {
        none: { per_sq_in: 0, setup: 0 },
        one_color: { per_sq_in: 0.0003, setup: 200 },
        cmyk_outside: { per_sq_in: 0.0006, setup: 300 },
      },
      default_print: "cmyk_outside",
      finishes: [],
      addons: ["zipper"],
      structural_setup: 0,
      production_days: [12, 18],
      volume_exponent: 0.18,
      volume_floor: 0.5,
      keywords: [
        "pouch",
        "stand up pouch",
        "stand-up pouch",
        "doypack",
        "bag with zipper",
        "coffee bag",
        "snack bag",
        "kraft pouch",
      ],
      description:
        "Printed laminated stand-up pouch for coffee, tea, snacks, supplements and pet treats.",
    },
    "paper-bag": {
      id: "paper-bag",
      label: "Custom Paper Shopping Bag",
      category: "Bags",
      instant: true,
      shape: "bag",
      moq: 1000,
      max_quantity: 200000,
      dimension_labels: ["Width", "Gusset", "Height"],
      default_dimensions: [10, 5, 13],
      min_dimensions: [4, 2, 5],
      max_dimensions: [24, 10, 24],
      blank_factor: 1.1,
      materials: [
        { id: "kraft_120gsm", label: "Kraft 120gsm", per_sq_in: 0.0007 },
        {
          id: "white_art_157gsm",
          label: "White art paper 157gsm (laminated)",
          per_sq_in: 0.0012,
        },
      ],
      default_material: "kraft_120gsm",
      base_unit_cost: 0.35,
      print: {
        none: { per_sq_in: 0, setup: 0 },
        one_color: { per_sq_in: 0.0006, setup: 150 },
        cmyk_outside: { per_sq_in: 0.0012, setup: 250 },
      },
      default_print: "one_color",
      finishes: ["matte_lamination", "gloss_lamination", "foil"],
      addons: [],
      structural_setup: 0,
      production_days: [12, 18],
      volume_exponent: 0.14,
      volume_floor: 0.6,
      keywords: [
        "paper bag",
        "shopping bag",
        "retail bag",
        "gift bag",
        "carrier bag",
        "tote",
      ],
      description:
        "Branded paper shopping bag with twisted or flat handles for retail and events.",
    },
    label: {
      id: "label",
      label: "Custom Labels & Stickers",
      category: "Labels",
      instant: true,
      shape: "flat",
      moq: 2000,
      max_quantity: 1000000,
      dimension_labels: ["Width", "Height"],
      default_dimensions: [3, 3],
      min_dimensions: [0.75, 0.75],
      max_dimensions: [12, 12],
      blank_factor: 1,
      materials: [
        {
          id: "white_bopp",
          label: "White BOPP (waterproof)",
          per_sq_in: 0.004,
        },
        { id: "paper_matte", label: "Matte paper", per_sq_in: 0.003 },
        { id: "clear_bopp", label: "Clear BOPP", per_sq_in: 0.0045 },
      ],
      default_material: "white_bopp",
      base_unit_cost: 0.01,
      print: {
        none: { per_sq_in: 0, setup: 0 },
        one_color: { per_sq_in: 0.0015, setup: 80 },
        cmyk_outside: { per_sq_in: 0.003, setup: 100 },
      },
      default_print: "cmyk_outside",
      finishes: ["gloss_lamination", "matte_lamination", "foil"],
      addons: [],
      structural_setup: 0,
      production_days: [5, 8],
      volume_exponent: 0.22,
      volume_floor: 0.45,
      keywords: ["label", "sticker", "stickers", "labels", "decal", "seal"],
      description:
        "Roll or sheet labels and stickers for products, jars, bottles and boxes.",
    },
    "tissue-paper": {
      id: "tissue-paper",
      label: "Custom Printed Tissue Paper",
      category: "Accessories",
      instant: true,
      shape: "flat",
      moq: 10000,
      max_quantity: 1000000,
      dimension_labels: ["Width", "Height"],
      default_dimensions: [20, 30],
      min_dimensions: [10, 10],
      max_dimensions: [30, 40],
      blank_factor: 1,
      materials: [
        {
          id: "tissue_17gsm",
          label: "17gsm acid-free tissue",
          per_sq_in: 0.00012,
        },
        {
          id: "tissue_food_grade",
          label: "Food-grade tissue",
          per_sq_in: 0.00015,
        },
      ],
      default_material: "tissue_17gsm",
      base_unit_cost: 0.005,
      print: {
        one_color: { per_sq_in: 0.00008, setup: 200 },
        cmyk_outside: { per_sq_in: 0.00016, setup: 300 },
      },
      default_print: "one_color",
      finishes: [],
      addons: [],
      structural_setup: 0,
      production_days: [10, 15],
      volume_exponent: 0.15,
      volume_floor: 0.55,
      keywords: ["tissue", "tissue paper", "wrapping paper", "wrap"],
      description:
        "Branded tissue wrapping paper for apparel, gifts and unboxing experiences.",
    },
    "box-insert": {
      id: "box-insert",
      label: "Custom Box Insert",
      category: "Inserts",
      instant: true,
      shape: "flat",
      moq: 1000,
      max_quantity: 200000,
      dimension_labels: ["Length", "Width"],
      default_dimensions: [8, 6],
      min_dimensions: [2, 2],
      max_dimensions: [24, 20],
      blank_factor: 1.2,
      materials: [
        {
          id: "paperboard_insert",
          label: "Paperboard die-cut insert",
          per_sq_in: 0.0012,
        },
        {
          id: "corrugated_insert",
          label: "Corrugated die-cut insert",
          per_sq_in: 0.0016,
        },
      ],
      default_material: "paperboard_insert",
      base_unit_cost: 0.05,
      print: {
        none: { per_sq_in: 0, setup: 0 },
        one_color: { per_sq_in: 0.0006, setup: 150 },
      },
      default_print: "none",
      finishes: [],
      addons: [],
      structural_setup: 150,
      production_days: [8, 12],
      volume_exponent: 0.15,
      volume_floor: 0.6,
      keywords: ["insert", "inserts", "divider", "cradle", "partition"],
      description:
        "Die-cut paperboard or corrugated insert that holds products in place.",
    },
    "poly-mailer": {
      id: "poly-mailer",
      label: "Custom Poly Mailer",
      category: "Mailers",
      instant: true,
      shape: "bag",
      moq: 5000,
      max_quantity: 1000000,
      dimension_labels: ["Width", "Gusset", "Height"],
      default_dimensions: [10, 0, 13],
      min_dimensions: [6, 0, 6],
      max_dimensions: [24, 4, 30],
      blank_factor: 1,
      materials: [
        {
          id: "ldpe_60mic",
          label: "LDPE 60 micron (30% recycled)",
          per_sq_in: 0.00035,
        },
        {
          id: "compostable_film",
          label: "Compostable PBAT/PLA film",
          per_sq_in: 0.0007,
        },
      ],
      default_material: "ldpe_60mic",
      base_unit_cost: 0.03,
      print: {
        none: { per_sq_in: 0, setup: 0 },
        one_color: { per_sq_in: 0.0004, setup: 200 },
        cmyk_outside: { per_sq_in: 0.0009, setup: 300 },
      },
      default_print: "one_color",
      finishes: [],
      addons: [],
      structural_setup: 0,
      production_days: [10, 15],
      volume_exponent: 0.16,
      volume_floor: 0.55,
      keywords: [
        "poly mailer",
        "polymailer",
        "mailing bag",
        "courier bag",
        "plastic mailer",
        "compostable mailer",
      ],
      description:
        "Printed self-seal poly mailer bags for apparel and soft goods.",
    },
    "packing-tape": {
      id: "packing-tape",
      label: "Custom Printed Packing Tape",
      category: "Accessories",
      instant: true,
      shape: "unit",
      moq: 500,
      max_quantity: 100000,
      dimension_labels: [],
      default_dimensions: [],
      min_dimensions: [],
      max_dimensions: [],
      blank_factor: 1,
      materials: [
        { id: "bopp_2in_110yd", label: 'BOPP 2" x 110 yd', per_sq_in: 0 },
        {
          id: "kraft_2in_110yd",
          label: 'Water-activated kraft 3" x 450 ft',
          per_sq_in: 0,
        },
      ],
      default_material: "bopp_2in_110yd",
      base_unit_cost: 2.1,
      print: {
        one_color: { per_sq_in: 0, per_unit: 0, setup: 120 },
        cmyk_outside: { per_sq_in: 0, per_unit: 0.6, setup: 200 },
      },
      default_print: "one_color",
      finishes: [],
      addons: [],
      structural_setup: 0,
      production_days: [7, 10],
      volume_exponent: 0.12,
      volume_floor: 0.7,
      keywords: ["tape", "packing tape", "packaging tape", "branded tape"],
      description: "Branded packing tape rolls for sealing shipping boxes.",
    },
    "floor-display": {
      id: "floor-display",
      label: "Corrugated Floor Display",
      category: "Displays",
      instant: false,
      shape: "box",
      moq: 100,
      max_quantity: 10000,
      dimension_labels: ["Width", "Depth", "Height"],
      default_dimensions: [16, 14, 60],
      min_dimensions: [8, 8, 20],
      max_dimensions: [48, 36, 84],
      blank_factor: 1.6,
      materials: [
        {
          id: "corrugated_display",
          label: "Corrugated display board",
          per_sq_in: 0.0025,
        },
      ],
      default_material: "corrugated_display",
      base_unit_cost: 6,
      print: { cmyk_outside: { per_sq_in: 0.0025, setup: 400 } },
      default_print: "cmyk_outside",
      finishes: [],
      addons: [],
      structural_setup: 400,
      production_days: [15, 25],
      volume_exponent: 0.12,
      volume_floor: 0.7,
      keywords: [
        "display",
        "floor display",
        "pdq",
        "counter display",
        "pop display",
        "standee",
      ],
      description:
        "Retail floor / counter displays need a structural review, so PackOasis sends a reviewed quote within one business day.",
    },
    "tin-box": {
      id: "tin-box",
      label: "Custom Tin Box",
      category: "Tins",
      instant: false,
      shape: "box",
      moq: 1000,
      max_quantity: 100000,
      dimension_labels: ["Length", "Width", "Height"],
      default_dimensions: [4, 3, 1],
      min_dimensions: [1, 1, 0.5],
      max_dimensions: [12, 12, 8],
      blank_factor: 1.3,
      materials: [
        { id: "tinplate", label: "0.23mm tinplate", per_sq_in: 0.006 },
      ],
      default_material: "tinplate",
      base_unit_cost: 0.4,
      print: { cmyk_outside: { per_sq_in: 0.002, setup: 600 } },
      default_print: "cmyk_outside",
      finishes: [],
      addons: [],
      structural_setup: 1500,
      production_days: [25, 35],
      volume_exponent: 0.12,
      volume_floor: 0.7,
      keywords: ["tin", "tin box", "metal box", "tin can"],
      description:
        "Metal tins need custom tooling, so PackOasis sends a reviewed quote within one business day.",
    },
  },
}

const MEGA_MENU = "/media.packoasis.com/media_upload/coding_guide/mega-menu"

/** Storefront-relative preview images (mirror assets) per product type. */
export const PRODUCT_IMAGES: Record<string, string> = {
  "mailer-box": `${MEGA_MENU}/corrugated.jpg`,
  "shipping-box": `${MEGA_MENU}/corrugated.jpg`,
  "folding-carton": `${MEGA_MENU}/foldingcarton.jpg`,
  "rigid-box": `${MEGA_MENU}/rigid.jpg`,
  "stand-up-pouch": `${MEGA_MENU}/bag-4.jpg`,
  "paper-bag": `${MEGA_MENU}/bag-1.jpg`,
  label: `${MEGA_MENU}/sticker.jpg`,
  "box-insert": `${MEGA_MENU}/insert.jpg`,
  "poly-mailer": `${MEGA_MENU}/bag-3.jpg`,
  "floor-display": `${MEGA_MENU}/display.jpg`,
  "tin-box": `${MEGA_MENU}/tin.jpg`,
}

/**
 * Maps storefront URLs to the product type the widget pre-selects, so the
 * quote opens on the product the visitor is already looking at. First match
 * wins; patterns are matched against the lower-cased path.
 */
export const PAGE_HINTS: { pattern: string; product_type: string }[] = [
  {
    pattern: "poly-?mailer|mailer-bag|mailing-bag|custom-mailers",
    product_type: "poly-mailer",
  },
  { pattern: "tape", product_type: "packing-tape" },
  { pattern: "tissue", product_type: "tissue-paper" },
  { pattern: "label|sticker", product_type: "label" },
  { pattern: "insert", product_type: "box-insert" },
  { pattern: "display", product_type: "floor-display" },
  { pattern: "tin-box|custom-tin", product_type: "tin-box" },
  { pattern: "pouch|coffee|pet-packaging", product_type: "stand-up-pouch" },
  { pattern: "paper-bag|luxury-paper", product_type: "paper-bag" },
  {
    pattern:
      "rigid|setup-box|jewelry|wine|gift|luxury|promotional|presentation|electronics",
    product_type: "rigid-box",
  },
  {
    pattern:
      "mailer|shipping-box|ecommerce|subscription|apparel|fashion|automotive|eco-friendly",
    product_type: "mailer-box",
  },
  { pattern: "corrugated", product_type: "shipping-box" },
  {
    pattern:
      "folding-carton|paperboard|tuck-end|cosmetic|pharmaceutical|soap|tea|candy|chocolate|food|bakery|cannabis|retail|window|game|toy|candle|beverage|beer|restaurant|cbd",
    product_type: "folding-carton",
  },
]

/** Freemail providers: skip company-domain enrichment for these. */
export const FREEMAIL_DOMAINS = new Set([
  "gmail.com",
  "googlemail.com",
  "outlook.com",
  "hotmail.com",
  "live.com",
  "msn.com",
  "yahoo.com",
  "ymail.com",
  "icloud.com",
  "me.com",
  "mac.com",
  "aol.com",
  "proton.me",
  "protonmail.com",
  "gmx.com",
  "gmx.net",
  "mail.com",
  "zoho.com",
  "yandex.com",
  "yandex.ru",
  "mail.ru",
  "qq.com",
  "163.com",
  "126.com",
  "foxmail.com",
  "sina.com",
  "sohu.com",
  "hey.com",
  "fastmail.com",
])
