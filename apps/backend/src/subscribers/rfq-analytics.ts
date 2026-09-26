import { SubscriberArgs, SubscriberConfig } from "@medusajs/framework"
import { ContainerRegistrationKeys, Modules } from "@medusajs/framework/utils"

export default async function rfqAnalyticsHandler({
  event: { data },
  container,
}: SubscriberArgs<{ id: string }>) {
  const analyticsModuleService: any = container.resolve(Modules.ANALYTICS)
  const rfqModuleService: any = container.resolve("rfq")

  // Fetch the full RFQ details to get email and title for better tracking
  const rfq = await rfqModuleService.retrieveRFQ(data.id)

  try {
    await analyticsModuleService.track({
      event: "rfq_submitted",
      // PostHog requires a distinct id; the buyer email ties it to later events.
      actor_id: rfq.email || rfq.id,
      properties: {
        rfq_id: rfq.id,
        email: rfq.email,
        contact_name: rfq.contact_name,
        title: rfq.title,
        source: rfq.source,
        quantity: rfq.quantity,
        company: rfq.company,
      },
    })
  } catch (error) {
    container
      .resolve(ContainerRegistrationKeys.LOGGER)
      .warn(`[rfq-analytics] track failed: ${(error as Error).message}`)
  }
}

// Module services emit `<module>.<model>.<action>`, so RFQ creation is
// `rfq.rfq.created` (a plain `rfq.created` subscriber never fires).
export const config: SubscriberConfig = {
  event: "rfq.rfq.created",
}
