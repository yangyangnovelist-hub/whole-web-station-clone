import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { ContainerRegistrationKeys } from "@medusajs/framework/utils"
import { z } from "zod"
import {
  heuristicParseSpecs,
  parseSpecsWithAI,
} from "../../../../lib/ai/parse-specs"
import { priceQuote } from "../../../../lib/instant-quote/engine"
import { recordLeadEvent } from "../../../../lib/lead-intel/record"
import { PageSchema, validationError, VisitorIdSchema } from "../validators"

const ParseSchema = z.object({
  text: z.string().trim().min(3, "Describe your packaging").max(4000),
  product_type: z.string().max(64).optional(),
  visitor_id: VisitorIdSchema.optional(),
  page: PageSchema,
})

export async function POST(req: MedusaRequest, res: MedusaResponse) {
  const parsed = ParseSchema.safeParse(req.body)
  if (!parsed.success) {
    return res.status(400).json(validationError(parsed.error))
  }

  const logger = req.scope.resolve(ContainerRegistrationKeys.LOGGER)
  const { text, product_type } = parsed.data
  const specs =
    (await parseSpecsWithAI(text, product_type, logger)) ??
    heuristicParseSpecs(text, product_type)

  const quote = priceQuote(specs)

  if (parsed.data.visitor_id) {
    await recordLeadEvent(req.scope, parsed.data.visitor_id, {
      type: "quote_described",
      url: parsed.data.page?.url,
      path: parsed.data.page?.path,
      product_type: quote.product_type,
      payload: { text: text.slice(0, 500), source: specs.source },
    })
  }

  res.json({
    specs,
    source: specs.source,
    missing: specs.missing,
    notes: specs.notes,
    quote,
  })
}
