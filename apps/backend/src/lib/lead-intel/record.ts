import { MedusaContainer } from "@medusajs/framework/types"
import { ContainerRegistrationKeys } from "@medusajs/framework/utils"
import { LEAD_MODULE } from "../../modules/lead"
import type LeadModuleService from "../../modules/lead/service"
import type { LeadEventInput } from "../../modules/lead/service"

const lastRecorded = new Map<string, number>()

function truncate(value: string | null | undefined, max: number) {
  return value ? value.slice(0, max) : null
}

/** Stores a funnel event for a visitor; optional per-type throttle; never throws. */
export async function recordLeadEvent(
  container: MedusaContainer,
  visitorId: string,
  event: LeadEventInput & { throttleMs?: number }
) {
  const { throttleMs, ...data } = event
  if (throttleMs) {
    const key = `${visitorId}:${data.type}`
    const now = Date.now()
    if ((lastRecorded.get(key) ?? 0) > now - throttleMs) {
      return
    }
    lastRecorded.set(key, now)
    if (lastRecorded.size > 20_000) {
      lastRecorded.clear()
    }
  }

  try {
    const leadService: LeadModuleService = container.resolve(LEAD_MODULE)
    await leadService.recordEvents(visitorId, [
      {
        type: data.type.slice(0, 40),
        url: truncate(data.url, 500),
        path: truncate(data.path, 300),
        title: truncate(data.title, 200),
        referrer: truncate(data.referrer, 500),
        product_type: truncate(data.product_type, 64),
        payload: data.payload ?? null,
      },
    ])
  } catch (error) {
    container
      .resolve(ContainerRegistrationKeys.LOGGER)
      .warn(`[lead] could not record ${data.type}: ${(error as Error).message}`)
  }
}
