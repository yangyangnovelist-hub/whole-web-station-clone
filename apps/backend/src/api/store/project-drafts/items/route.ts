import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { z } from "zod"
import { PROJECT_DRAFT_MODULE } from "../../../../modules/project-draft"

const AddDraftItemSchema = z.object({
  draft_id: z.string().optional(),
  session_id: z.string().optional(),
  item: z.object({
    title: z.string().min(1, "Item title is required"),
    quantity: z.coerce.number().positive().default(1),
    product_handle: z.string().optional(),
    sku: z.string().optional(),
    url: z.string().optional(),
    image: z.string().optional(),
    notes: z.string().optional(),
    metadata: z.record(z.any()).optional(),
  }),
})

export async function POST(req: MedusaRequest, res: MedusaResponse) {
  const parsed = AddDraftItemSchema.safeParse(req.body)
  if (!parsed.success) {
    return res.status(400).json({
      code: "VALIDATION_ERROR",
      message: parsed.error.errors.map((error) => error.message).join(", "),
    })
  }

  const projectDraftService: any = req.scope.resolve(PROJECT_DRAFT_MODULE)
  const draft = await projectDraftService.addItem({
    draft_id:
      parsed.data.draft_id || req.get("x-packoasis-draft-id") || undefined,
    session_id:
      parsed.data.session_id || req.get("x-packoasis-session-id") || undefined,
    customer_id: (req as any).auth_context?.actor_id || null,
    source: "website",
    item: parsed.data.item,
  })

  return res.status(201).json({ draft })
}
