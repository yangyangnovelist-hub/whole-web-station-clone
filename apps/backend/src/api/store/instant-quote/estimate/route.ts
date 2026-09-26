import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { z } from "zod"
import {
  priceQuote,
  QuoteInputError,
} from "../../../../lib/instant-quote/engine"
import { recordLeadEvent } from "../../../../lib/lead-intel/record"
import {
  PageSchema,
  SpecsSchema,
  validationError,
  VisitorIdSchema,
} from "../validators"

const EstimateSchema = z.object({
  specs: SpecsSchema,
  visitor_id: VisitorIdSchema.optional(),
  page: PageSchema,
})

export async function POST(req: MedusaRequest, res: MedusaResponse) {
  const parsed = EstimateSchema.safeParse(req.body)
  if (!parsed.success) {
    return res.status(400).json(validationError(parsed.error))
  }

  let quote
  try {
    quote = priceQuote(parsed.data.specs)
  } catch (error) {
    if (error instanceof QuoteInputError) {
      return res
        .status(400)
        .json({ code: "INVALID_SPECS", message: error.message })
    }
    throw error
  }

  if (parsed.data.visitor_id) {
    await recordLeadEvent(req.scope, parsed.data.visitor_id, {
      type: "quote_priced",
      url: parsed.data.page?.url,
      path: parsed.data.page?.path,
      title: parsed.data.page?.title,
      product_type: quote.product_type,
      payload: {
        quantity: quote.quantity,
        total: quote.total,
        summary: quote.summary,
      },
      throttleMs: 30_000,
    })
  }

  res.json({ quote })
}
