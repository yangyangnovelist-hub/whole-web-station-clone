import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { Modules } from "@medusajs/framework/utils"
import { z } from "zod"
import { LEAD_CREATED_EVENT } from "../../../lib/events"
import { RFQ_MODULE } from "../../../modules/rfq"
import { PROJECT_DRAFT_MODULE } from "../../../modules/project-draft"

// Public endpoint: every free-text field is bounded.
const CreateRFQSchema = z.object({
  draft_id: z.string().max(100).optional(),
  source: z.string().max(64).optional(),
  contact_name: z.string().trim().min(1, "Contact name is required").max(120),
  email: z
    .string()
    .trim()
    .email("A valid email is required")
    .max(254)
    .toLowerCase(),
  company: z.string().max(200).optional(),
  phone: z.string().max(200).optional(),
  title: z.string().trim().min(1, "Title is required").max(200),
  application: z.string().max(200).optional(),
  quantity: z.coerce.number().int().positive().optional(),
  target_price: z.coerce.number().positive().optional(),
  need_by: z.string().datetime({ message: "need_by must be a valid ISO date" }).optional(),
  notes: z.string().max(5000).optional(),
  website: z.string().max(200).optional(),
  visitor_id: z
    .string()
    .regex(/^[A-Za-z0-9_-]{8,64}$/)
    .optional(),
  items: z
    .array(
      z.object({
        id: z.string().max(100).optional(),
        title: z.string().max(200),
        quantity: z.coerce.number().positive().optional(),
        product_handle: z.string().max(200).optional(),
        sku: z.string().max(100).optional(),
        url: z.string().max(2000).optional(),
        image: z.string().max(2000).optional(),
        notes: z.string().max(5000).optional(),
        metadata: z.record(z.any()).optional(),
      })
    )
    .max(50)
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
    website: parsed.data.website ?? null,
    visitor_id: parsed.data.visitor_id ?? null,
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

  await req.scope
    .resolve(Modules.EVENT_BUS)
    .emit({ name: LEAD_CREATED_EVENT, data: { id: rfq.id } })

  return res.status(201).json({ rfq })
}
