import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { priceQuote, QuoteInputError } from "../../../lib/instant-quote/engine"
import { packoasisConfig } from "../../../lib/packoasis-config"

function list(value: unknown) {
  return typeof value === "string" && value
    ? value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean)
    : undefined
}

/**
 * Keyless, read-only instant price for AI assistants and partners, e.g.
 * GET /packoasis/quote?product_type=mailer-box&dimensions=10x8x4&quantity=1000
 * The response carries an `order_url` that opens the storefront quote widget
 * pre-filled, so an assistant can hand the buyer a one-click path to order.
 */
export async function GET(req: MedusaRequest, res: MedusaResponse) {
  res.setHeader("Access-Control-Allow-Origin", "*")
  const q = req.query as Record<string, unknown>
  const productType = String(q.product_type ?? q.product ?? "")
  if (!productType) {
    return res.status(400).json({
      code: "INVALID_SPECS",
      message: "product_type is required, see /packoasis/catalog",
    })
  }

  const specs = {
    product_type: productType,
    dimensions: list(
      typeof q.dimensions === "string"
        ? q.dimensions.replace(/x/gi, ",")
        : undefined
    )?.map(Number),
    unit: (["in", "cm", "mm"].includes(String(q.unit)) ? q.unit : undefined) as
      | "in"
      | "cm"
      | "mm"
      | undefined,
    quantity: q.quantity ? Number(q.quantity) : undefined,
    material: typeof q.material === "string" ? q.material : undefined,
    print: typeof q.print === "string" ? q.print : undefined,
    finishes: list(q.finishes),
    addons: list(q.addons),
    rush: q.rush === "true" || q.rush === "1",
  }

  try {
    const quote = priceQuote(specs)
    const encoded = Buffer.from(JSON.stringify(quote.specs)).toString(
      "base64url"
    )
    res.setHeader("Cache-Control", "public, max-age=60")
    res.json({
      quote,
      order_url: `${packoasisConfig.storefrontUrl()}/?po_quote=${encoded}#instant-quote`,
    })
  } catch (error) {
    if (error instanceof QuoteInputError) {
      return res
        .status(400)
        .json({ code: "INVALID_SPECS", message: error.message })
    }
    throw error
  }
}
