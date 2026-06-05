import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { z } from "zod"
import { RFQ_MODULE } from "../../../modules/rfq"
import { PROJECT_DRAFT_MODULE } from "../../../modules/project-draft"

const CreateRFQSchema = z.object({
  draft_id: z.string().optional(),
  source: z.string().optional(),
  contact_name: z.string().min(1, "Contact name is required"),
  email: z.string().email("A valid email is required"),
  company: z.string().optional(),
  phone: z.string().optional(),
  title: z.string().min(1, "Title is required"),
  application: z.string().optional(),
  quantity: z.coerce.number().int().positive().optional(),
  target_price: z.coerce.number().positive().optional(),
  need_by: z.string().datetime({ message: "need_by must be a valid ISO date" }).optional(),
  notes: z.string().optional(),
  items: z
    .array(
      z.object({
        id: z.string().optional(),
        title: z.string(),
        quantity: z.coerce.number().positive().optional(),
        product_handle: z.string().optional(),
        sku: z.string().optional(),
        url: z.string().optional(),
        image: z.string().optional(),
        notes: z.string().optional(),
        metadata: z.record(z.any()).optional(),
      })
    )
    .optional(),
})

export async function POST(req: MedusaRequest, res: MedusaResponse) {
  const parsed = CreateRFQSchema.safeParse(req.body)
  if (!parsed.success) {
    return res.status(400).json({
      code: "VALIDATION_ERROR",
      message: parsed.error.errors.map((error) => error.message).join(", "),
    })
  }

  const rfqService: any = req.scope.resolve(RFQ_MODULE)
  const projectDraftService: any = req.scope.resolve(PROJECT_DRAFT_MODULE)

  let draft: any = null
  let items: any[] = parsed.data.items || []

  if (parsed.data.draft_id) {
    draft = await projectDraftService.getCurrentDraft({
      draft_id: parsed.data.draft_id,
    })

    if (!draft) {
      return res.status(404).json({
        code: "DRAFT_NOT_FOUND",
        message: "Project draft not found",
      })
    }

    if (!items.length) {
      items = draft.items || []
    }
  }

  const normalizedQuantity =
    parsed.data.quantity ||
    items.reduce(
      (total, item) => total + (Number(item.quantity || 1) || 1),
      0
    ) ||
    1

  const rfq = await rfqService.submitRFQ({
    buyer_id: (req as any).auth_context?.actor_id || null,
    draft_id: parsed.data.draft_id || null,
    source: parsed.data.source || (parsed.data.draft_id ? "project_draft" : "contact_form"),
    contact_name: parsed.data.contact_name,
    email: parsed.data.email,
    company: parsed.data.company,
    phone: parsed.data.phone,
    title: parsed.data.title,
    application: parsed.data.application,
    quantity: normalizedQuantity,
    target_price: parsed.data.target_price,
    need_by: parsed.data.need_by ? new Date(parsed.data.need_by) : undefined,
    notes: parsed.data.notes,
    items_payload: items.length ? JSON.stringify(items) : null,
  })

  if (parsed.data.draft_id) {
    await projectDraftService.markSubmitted({
      draft_id: parsed.data.draft_id,
      contact_name: parsed.data.contact_name,
      email: parsed.data.email,
      company: parsed.data.company,
      phone: parsed.data.phone,
      notes: parsed.data.notes,
    })
  }

  return res.status(201).json({ rfq })
}
