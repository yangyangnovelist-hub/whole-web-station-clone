import {
  AddonId,
  DEFAULT_PRICING,
  FinishId,
  PAGE_HINTS,
  PricingConfig,
  PRODUCT_IMAGES,
  PrintOptionId,
  ProductTypeConfig,
} from "./catalog"

export type DimensionUnit = "in" | "cm" | "mm"

export type QuoteInput = {
  product_type: string
  dimensions?: number[] | null
  unit?: DimensionUnit | null
  quantity?: number | null
  material?: string | null
  print?: string | null
  finishes?: string[] | null
  addons?: string[] | null
  rush?: boolean | null
}

export type NormalizedSpecs = {
  product_type: string
  dimensions: number[]
  unit: "in"
  quantity: number
  material: string
  print: PrintOptionId
  finishes: FinishId[]
  addons: AddonId[]
  rush: boolean
}

export type QuoteTier = {
  quantity: number
  unit_price: number
  total: number
  savings_pct: number
}

export type QuoteResult = {
  pricing_version: string
  instant: boolean
  product_type: string
  product_label: string
  summary: string
  specs: NormalizedSpecs
  spec_labels: {
    dimensions: string
    material: string
    print: string
    finishes: string[]
    addons: string[]
  }
  currency_code: string
  quantity: number
  unit_price: number
  total: number
  freight_included: boolean
  breakdown: { label: string; amount: number }[]
  tiers: QuoteTier[]
  moq: number
  lead_time: {
    production_days: [number, number]
    shipping_days: [number, number]
    rush: boolean
  }
  estimated_delivery: { from: string; to: string }
  valid_until: string
  assumptions: string[]
  warnings: string[]
}

export class QuoteInputError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "QuoteInputError"
  }
}

type PlainObject = Record<string, unknown>

function isPlainObject(value: unknown): value is PlainObject {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

/**
 * Own-key check for lookups keyed by client input, so ids such as
 * "constructor" or "__proto__" never resolve to Object.prototype members.
 */
export function hasOwn<T extends object>(
  object: T,
  key: string
): key is Extract<keyof T, string> {
  return Object.prototype.hasOwnProperty.call(object, key)
}

export function deepMerge<T>(base: T, override: unknown): T {
  if (!isPlainObject(base) || !isPlainObject(override)) {
    return (override === undefined ? base : override) as T
  }

  const result: PlainObject = { ...base }
  for (const [key, value] of Object.entries(override)) {
    if (
      value === null &&
      (!hasOwn(result, key) || isPlainObject(result[key]))
    ) {
      // null removes an entry, e.g. {"print":{"cmyk_both":null}} disables it
      delete result[key]
      continue
    }
    result[key] = hasOwn(result, key) ? deepMerge(result[key], value) : value
  }

  return result as T
}

/**
 * An override can remove print options ({"print":{"one_color":null}}). Keep
 * only real rates, point default_print at one of them, and drop a product
 * type that has no print option left.
 */
function withAvailablePrints(config: PricingConfig): PricingConfig {
  const productTypes: Record<string, ProductTypeConfig> = {}
  let changed = false
  for (const [id, type] of Object.entries(config.product_types)) {
    const rates = Object.entries(type.print).filter(([, rate]) =>
      isPlainObject(rate)
    )
    const ids = rates.map(([printId]) => printId as PrintOptionId)
    const defaultPrint = ids.includes(type.default_print)
      ? type.default_print
      : ids[0]
    if (
      defaultPrint === type.default_print &&
      rates.length === Object.keys(type.print).length
    ) {
      productTypes[id] = type
      continue
    }

    changed = true
    if (!defaultPrint) {
      console.error(
        `[instant-quote] INSTANT_QUOTE_PRICING_JSON leaves ${id} without a print option; it is not offered.`
      )
      continue
    }
    if (defaultPrint !== type.default_print) {
      console.error(
        `[instant-quote] INSTANT_QUOTE_PRICING_JSON removes the default print "${type.default_print}" of ${id}; using "${defaultPrint}".`
      )
    }
    productTypes[id] = {
      ...type,
      print: Object.fromEntries(rates),
      default_print: defaultPrint,
    }
  }

  return changed ? { ...config, product_types: productTypes } : config
}

let cachedConfig: { raw: string | undefined; config: PricingConfig } | null =
  null

/**
 * DEFAULT_PRICING deep-merged with INSTANT_QUOTE_PRICING_JSON so prices can be
 * calibrated per environment without a code change.
 */
export function loadPricingConfig(
  raw: string | undefined = process.env.INSTANT_QUOTE_PRICING_JSON
): PricingConfig {
  if (cachedConfig && cachedConfig.raw === raw) {
    return cachedConfig.config
  }

  let config = DEFAULT_PRICING
  if (raw && raw.trim()) {
    try {
      config = withAvailablePrints(deepMerge(DEFAULT_PRICING, JSON.parse(raw)))
    } catch (error) {
      console.error(
        `[instant-quote] INSTANT_QUOTE_PRICING_JSON is not valid JSON, using defaults: ${
          (error as Error).message
        }`
      )
    }
  }

  cachedConfig = { raw, config }
  return config
}

const UNIT_TO_INCH: Record<DimensionUnit, number> = {
  in: 1,
  cm: 1 / 2.54,
  mm: 1 / 25.4,
}

function round(value: number, digits: number) {
  const factor = 10 ** digits
  return Math.round((value + Number.EPSILON) * factor) / factor
}

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value))
}

function formatNumber(value: number) {
  return Number.isInteger(value) ? String(value) : String(round(value, 2))
}

export function getProductType(
  id: string,
  config: PricingConfig = loadPricingConfig()
): ProductTypeConfig | undefined {
  return hasOwn(config.product_types, id) ? config.product_types[id] : undefined
}

export function normalizeSpecs(
  input: QuoteInput,
  config: PricingConfig = loadPricingConfig()
): { specs: NormalizedSpecs; type: ProductTypeConfig; warnings: string[] } {
  const type = getProductType(input.product_type, config)
  if (!type) {
    throw new QuoteInputError(`Unknown product type "${input.product_type}"`)
  }

  const warnings: string[] = []
  const unit: DimensionUnit =
    input.unit && hasOwn(UNIT_TO_INCH, input.unit) ? input.unit : "in"

  const dimensions = type.default_dimensions.map((fallback, index) => {
    const raw = input.dimensions?.[index]
    if (raw === undefined || raw === null || !Number.isFinite(Number(raw))) {
      return fallback
    }

    const inches = round(Number(raw) * UNIT_TO_INCH[unit], 2)
    const min = type.min_dimensions[index]
    const max = type.max_dimensions[index]
    const bounded = clamp(inches, min, max)
    if (bounded !== inches) {
      warnings.push(
        `${type.dimension_labels[index]} adjusted to ${formatNumber(
          bounded
        )} in (supported range ${formatNumber(min)}-${formatNumber(max)} in).`
      )
    }
    return bounded
  })

  let quantity = Math.round(Number(input.quantity ?? type.moq))
  if (!Number.isFinite(quantity) || quantity <= 0) {
    quantity = type.moq
  }
  if (quantity < type.moq) {
    warnings.push(
      `Quantity raised to the ${type.moq.toLocaleString("en-US")} piece minimum for ${type.label}.`
    )
    quantity = type.moq
  }
  if (quantity > type.max_quantity) {
    warnings.push(
      `Quantity capped at ${type.max_quantity.toLocaleString(
        "en-US"
      )}; contact PackOasis for larger programs.`
    )
    quantity = type.max_quantity
  }

  const material = type.materials.some((m) => m.id === input.material)
    ? (input.material as string)
    : type.default_material

  let print = type.default_print
  if (input.print) {
    const rate = hasOwn(type.print, input.print)
      ? type.print[input.print]
      : undefined
    if (isPlainObject(rate)) {
      print = input.print as PrintOptionId
    } else {
      const label = hasOwn(config.print_labels, input.print)
        ? config.print_labels[input.print]
        : input.print
      warnings.push(
        `${label} is not available for ${type.label}; using ${config.print_labels[type.default_print]}.`
      )
    }
  }

  const finishes = Array.from(new Set(input.finishes ?? [])).filter(
    (finish): finish is FinishId => {
      const known = hasOwn(config.finishes, finish)
      const allowed = known && type.finishes.includes(finish as FinishId)
      if (!allowed && known) {
        warnings.push(
          `${config.finishes[finish as FinishId].label} is not available for ${type.label}.`
        )
      }
      return allowed
    }
  )
  if (
    finishes.includes("matte_lamination") &&
    finishes.includes("gloss_lamination")
  ) {
    finishes.splice(finishes.indexOf("gloss_lamination"), 1)
    warnings.push("Matte and gloss lamination cannot be combined; kept matte.")
  }
  if (finishes.includes("soft_touch")) {
    for (const lamination of [
      "matte_lamination",
      "gloss_lamination",
    ] as const) {
      if (finishes.includes(lamination)) {
        finishes.splice(finishes.indexOf(lamination), 1)
      }
    }
  }

  const addons = Array.from(new Set(input.addons ?? [])).filter(
    (addon): addon is AddonId =>
      hasOwn(config.addons, addon) && type.addons.includes(addon as AddonId)
  )

  return {
    type,
    warnings,
    specs: {
      product_type: type.id,
      dimensions,
      unit: "in",
      quantity,
      material,
      print,
      finishes,
      addons,
      rush: Boolean(input.rush),
    },
  }
}

function surfaceArea(type: ProductTypeConfig, dims: number[]) {
  const [a = 0, b = 0, c = 0] = dims
  switch (type.shape) {
    case "box":
      return 2 * (a * b + a * c + b * c)
    case "bag":
      // width, gusset, height: front/back panels, side gussets and bottom
      return 2 * (a * c) + 2 * (b * c) + a * b
    case "pouch":
      // width, height, bottom gusset
      return 2 * (a * b) + a * c
    case "flat":
      return a * b
    default:
      return 0
  }
}

type CostModel = {
  variableUnitCost: number
  setupCost: number
  lines: { label: string; perUnit: number; setup: number }[]
  extraDays: number
}

function buildCostModel(
  specs: NormalizedSpecs,
  type: ProductTypeConfig,
  config: PricingConfig
): CostModel {
  const lines: CostModel["lines"] = []
  const surface = surfaceArea(type, specs.dimensions)
  const blank = surface * type.blank_factor
  const material =
    type.materials.find((m) => m.id === specs.material) ?? type.materials[0]

  lines.push({
    label: `${material.label}${
      blank ? ` (${Math.round(blank)} sq in blank)` : ""
    }`,
    perUnit: blank * material.per_sq_in + type.base_unit_cost,
    setup: 0,
  })

  const print = type.print[specs.print]
  if (print && (print.per_sq_in || print.per_unit || print.setup)) {
    lines.push({
      label: config.print_labels[specs.print],
      perUnit: surface * print.per_sq_in + (print.per_unit ?? 0),
      setup: print.setup,
    })
  }

  let extraDays = 0
  for (const finishId of specs.finishes) {
    const finish = config.finishes[finishId]
    lines.push({
      label: finish.label,
      perUnit: surface * (finish.per_sq_in ?? 0) + (finish.per_unit ?? 0),
      setup: finish.setup ?? 0,
    })
    extraDays = Math.max(extraDays, finish.extra_days ?? 0)
  }

  for (const addonId of specs.addons) {
    const addon = config.addons[addonId]
    const [a = 0, b = 0] = specs.dimensions
    lines.push({
      label: addon.label,
      perUnit: a * b * (addon.per_sq_in ?? 0) + (addon.per_unit ?? 0),
      setup: addon.setup ?? 0,
    })
  }

  if (type.structural_setup) {
    lines.push({
      label: "Cutting die & tooling",
      perUnit: 0,
      setup: type.structural_setup,
    })
  }

  return {
    lines,
    variableUnitCost: lines.reduce((sum, line) => sum + line.perUnit, 0),
    setupCost: lines.reduce((sum, line) => sum + line.setup, 0),
    extraDays,
  }
}

function volumeFactor(type: ProductTypeConfig, quantity: number) {
  return Math.max(
    type.volume_floor,
    Math.pow(quantity / type.moq, -type.volume_exponent)
  )
}

function priceAtQuantity(
  model: CostModel,
  type: ProductTypeConfig,
  config: PricingConfig,
  quantity: number,
  rush: boolean
) {
  const cost =
    model.variableUnitCost * volumeFactor(type, quantity) * quantity +
    model.setupCost
  // raw is the price before the order minimum is applied
  const raw = round(
    cost * config.margin_multiplier * (rush ? config.rush_multiplier : 1),
    2
  )
  const total = Math.max(config.min_order_total, raw)

  return { raw, total, unit_price: round(total / quantity, 4) }
}

function addBusinessDays(start: Date, days: number) {
  const date = new Date(start.getTime())
  let remaining = days
  while (remaining > 0) {
    date.setUTCDate(date.getUTCDate() + 1)
    const day = date.getUTCDay()
    if (day !== 0 && day !== 6) {
      remaining--
    }
  }
  return date
}

function isoDate(date: Date) {
  return date.toISOString().slice(0, 10)
}

/** The requested quantity, the next volume breaks above it, and the MOQ. */
function tierQuantities(type: ProductTypeConfig, requested: number) {
  const larger = [type.moq * 2, type.moq * 5, type.moq * 10, requested * 2]
    .filter((qty) => qty > requested && qty <= type.max_quantity)
    .sort((a, b) => a - b)
  const tiers = Array.from(new Set([requested, ...larger])).slice(0, 4)
  return requested > type.moq ? [type.moq, ...tiers] : tiers
}

export function priceQuote(
  input: QuoteInput,
  options: { config?: PricingConfig; now?: Date } = {}
): QuoteResult {
  const config = options.config ?? loadPricingConfig()
  const now = options.now ?? new Date()
  const { specs, type, warnings } = normalizeSpecs(input, config)
  const model = buildCostModel(specs, type, config)
  const { raw, total, unit_price } = priceAtQuantity(
    model,
    type,
    config,
    specs.quantity,
    specs.rush
  )

  const factor = volumeFactor(type, specs.quantity)
  const scale =
    config.margin_multiplier * (specs.rush ? config.rush_multiplier : 1)
  const breakdown = model.lines.map((line) => ({
    label: line.label,
    amount: round(
      (line.perUnit * factor * specs.quantity + line.setup) * scale,
      2
    ),
  }))
  const breakdownSum = breakdown.reduce((sum, line) => sum + line.amount, 0)
  if (total > raw) {
    breakdown.push({
      label: "Minimum order adjustment",
      amount: round(total - breakdownSum, 2),
    })
  } else {
    // Lines are rounded one by one; put the leftover cents on the largest
    // line so the breakdown always adds up to the total.
    const residue = round(total - breakdownSum, 2)
    if (residue !== 0) {
      const largest = breakdown.reduce((max, line) =>
        line.amount > max.amount ? line : max
      )
      largest.amount = round(largest.amount + residue, 2)
    }
  }

  const tiers = tierQuantities(type, specs.quantity).map((quantity) => {
    const tier = priceAtQuantity(model, type, config, quantity, specs.rush)
    return {
      quantity,
      unit_price: tier.unit_price,
      total: tier.total,
      savings_pct: round((1 - tier.unit_price / unit_price) * 100, 1),
    }
  })

  const production: [number, number] = [
    type.production_days[0] + model.extraDays,
    type.production_days[1] + model.extraDays,
  ]
  const productionDays: [number, number] = specs.rush
    ? [
        Math.max(3, Math.ceil(production[0] * config.rush_lead_time_factor)),
        Math.max(4, Math.ceil(production[1] * config.rush_lead_time_factor)),
      ]
    : production
  // One business day for the digital proof before production starts.
  const deliveryFrom = addBusinessDays(
    now,
    1 + productionDays[0] + config.shipping_days[0]
  )
  const deliveryTo = addBusinessDays(
    now,
    1 + productionDays[1] + config.shipping_days[1]
  )
  const validUntil = new Date(now.getTime())
  validUntil.setUTCDate(validUntil.getUTCDate() + config.quote_valid_days)

  const material =
    type.materials.find((m) => m.id === specs.material) ?? type.materials[0]
  const dimensionsLabel = specs.dimensions.length
    ? `${specs.dimensions.map(formatNumber).join(" x ")} in (${type.dimension_labels.join(" x ")})`
    : "Standard size"
  const finishLabels = specs.finishes.map((id) => config.finishes[id].label)
  const addonLabels = specs.addons.map((id) => config.addons[id].label)
  const summary = [
    type.label,
    specs.dimensions.length
      ? `${specs.dimensions.map(formatNumber).join(" x ")} in`
      : null,
    config.print_labels[specs.print],
    ...finishLabels,
    `${specs.quantity.toLocaleString("en-US")} pcs`,
  ]
    .filter(Boolean)
    .join(", ")

  const assumptions = [
    "Price includes one digital proof, cutting die / plates and QC inspection.",
    config.freight_included
      ? "Standard freight to one address in the contiguous US is included; other destinations are confirmed before production."
      : "Freight is quoted separately at checkout.",
    "Artwork is supplied print-ready (PDF/AI); PackOasis design help is available on request.",
    "Production starts after proof approval; dates are business days.",
  ]
  if (!type.instant) {
    assumptions.unshift(
      "This format needs a structural review: the figure is an indicative budget, and a specialist confirms the final price within one business day."
    )
  }

  return {
    pricing_version: config.version,
    instant: type.instant,
    product_type: type.id,
    product_label: type.label,
    summary,
    specs,
    spec_labels: {
      dimensions: dimensionsLabel,
      material: material.label,
      print: config.print_labels[specs.print],
      finishes: finishLabels,
      addons: addonLabels,
    },
    currency_code: config.currency_code,
    quantity: specs.quantity,
    unit_price,
    total,
    freight_included: config.freight_included,
    breakdown,
    tiers,
    moq: type.moq,
    lead_time: {
      production_days: productionDays,
      shipping_days: config.shipping_days,
      rush: specs.rush,
    },
    estimated_delivery: {
      from: isoDate(deliveryFrom),
      to: isoDate(deliveryTo),
    },
    valid_until: isoDate(validUntil),
    assumptions,
    warnings,
  }
}

export function productTypeForPath(path: string) {
  const lower = path.toLowerCase()
  return (
    PAGE_HINTS.find((hint) => new RegExp(hint.pattern).test(lower))
      ?.product_type ?? null
  )
}

/** Public catalog for the widget, AI agents and llms.txt (no cost internals). */
export function publicCatalog(config: PricingConfig = loadPricingConfig()) {
  return {
    pricing_version: config.version,
    page_hints: PAGE_HINTS,
    currency_code: config.currency_code,
    freight_included: config.freight_included,
    print_options: config.print_labels,
    finishes: Object.values(config.finishes).map(({ id, label }) => ({
      id,
      label,
    })),
    addons: Object.values(config.addons).map(({ id, label }) => ({
      id,
      label,
    })),
    product_types: Object.values(config.product_types).map((type) => {
      const starting = priceQuote(
        {
          product_type: type.id,
          quantity:
            type.moq * 10 <= type.max_quantity ? type.moq * 10 : type.moq,
          print: type.default_print,
        },
        { config }
      )
      const atMoq = priceQuote(
        {
          product_type: type.id,
          quantity: type.moq,
          print: type.default_print,
        },
        { config }
      )
      return {
        id: type.id,
        label: type.label,
        category: type.category,
        description: type.description,
        instant: type.instant,
        moq: type.moq,
        max_quantity: type.max_quantity,
        dimension_labels: type.dimension_labels,
        default_dimensions: type.default_dimensions,
        min_dimensions: type.min_dimensions,
        max_dimensions: type.max_dimensions,
        materials: type.materials.map(({ id, label }) => ({ id, label })),
        default_material: type.default_material,
        print_options: Object.keys(type.print),
        default_print: type.default_print,
        finishes: type.finishes,
        addons: type.addons,
        production_days: type.production_days,
        keywords: type.keywords,
        image: PRODUCT_IMAGES[type.id] ?? null,
        price_from: {
          unit_price: starting.unit_price,
          quantity: starting.quantity,
        },
        price_at_moq: { unit_price: atMoq.unit_price, total: atMoq.total },
      }
    }),
  }
}
