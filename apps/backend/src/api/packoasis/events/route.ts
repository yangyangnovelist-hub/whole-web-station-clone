import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { ContainerRegistrationKeys } from "@medusajs/framework/utils"
import { z } from "zod"
import { trustedStorefrontOrigin } from "../../../lib/http"
import { LEAD_MODULE } from "../../../modules/lead"
import type LeadModuleService from "../../../modules/lead/service"

const EventSchema = z.object({
  t: z.string().regex(/^[a-z_]{3,40}$/),
  u: z.string().max(500).optional(),
  p: z.string().max(300).optional(),
  ti: z.string().max(200).optional(),
  r: z.string().max(500).optional(),
  pt: z.string().max(64).optional(),
  d: z.record(z.unknown()).optional(),
})

const BeaconSchema = z.object({
  v: z.string().regex(/^[A-Za-z0-9_-]{8,64}$/),
  e: z.array(EventSchema).min(1).max(20),
})

/**
 * Browsing beacon from the storefront widget (navigator.sendBeacon, so the
 * body arrives as text/plain). Only accepted from our own storefront origins.
 */
export async function POST(req: MedusaRequest, res: MedusaResponse) {
  if (!trustedStorefrontOrigin(req)) {
    return res.status(204).end()
  }

  let body: unknown = req.body
  if (typeof body === "string") {
    try {
      body = JSON.parse(body)
    } catch {
      return res
        .status(400)
        .json({ code: "INVALID_JSON", message: "Invalid JSON" })
    }
  }

  const parsed = BeaconSchema.safeParse(body)
  if (!parsed.success) {
    return res
      .status(400)
      .json({ code: "VALIDATION_ERROR", message: "Invalid beacon" })
  }

  try {
    const leadService: LeadModuleService = req.scope.resolve(LEAD_MODULE)
    await leadService.recordEvents(
      parsed.data.v,
      parsed.data.e.map((event) => ({
        type: event.t,
        url: event.u ?? null,
        path: event.p ?? null,
        title: event.ti ?? null,
        referrer: event.r ?? null,
        product_type: event.pt ?? null,
        payload:
          event.d && JSON.stringify(event.d).length <= 2000 ? event.d : null,
      }))
    )
  } catch (error) {
    req.scope
      .resolve(ContainerRegistrationKeys.LOGGER)
      .warn(`[lead] beacon failed: ${(error as Error).message}`)
  }

  res.status(204).end()
}
